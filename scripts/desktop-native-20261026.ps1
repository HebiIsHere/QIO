# QIO 真机桌面验证原语（2026-10-26，工作流 D；一次性证据脚本，不参与产品构建）
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\desktop-native-20261026.ps1 -Action <name> ...
# 输出：单行 JSON（便于 Node 驱动解析）；失败时 json.ok=false 并带 error。
param(
  [Parameter(Mandatory=$true)][string]$Action,
  [int]$TargetPid = 0,
  [long]$Hwnd = 0,
  [string]$Path = "",
  [int]$Message = 0,
  [int]$WParam = 0,
  [int]$LParam = 0,
  [string]$Name = ""
)
$ErrorActionPreference = "Stop"
$source = @'
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class QioNative {
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder text, int max);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr hWnd, StringBuilder text, int max);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")] public static extern bool PostMessageW(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int cmd);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("ntdll.dll")] public static extern int NtSuspendProcess(IntPtr handle);
  [DllImport("kernel32.dll")] public static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
  [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr handle);
  public struct RECT { public int Left, Top, Right, Bottom; }
  public static List<IntPtr> WindowsOf(uint pid) {
    var list = new List<IntPtr>();
    EnumWindows(delegate(IntPtr h, IntPtr l) {
      uint p; GetWindowThreadProcessId(h, out p);
      if (p == pid) list.Add(h);
      return true;
    }, IntPtr.Zero);
    return list;
  }
  public static string ClassOf(IntPtr h) { var sb = new StringBuilder(256); GetClassNameW(h, sb, sb.Capacity); return sb.ToString(); }
  public static string TitleOf(IntPtr h) { var sb = new StringBuilder(512); GetWindowTextW(h, sb, sb.Capacity); return sb.ToString(); }
}
'@
Add-Type -TypeDefinition $source -Language CSharp | Out-Null

function Get-WindowInfo([long]$handle) {
  $h = [IntPtr]$handle
  $r = New-Object QioNative+RECT
  [QioNative]::GetWindowRect($h, [ref]$r) | Out-Null
  [pscustomobject]@{
    hwnd = $handle
    title = [QioNative]::TitleOf($h)
    class = [QioNative]::ClassOf($h)
    visible = [QioNative]::IsWindowVisible($h)
    rect = @{ left = $r.Left; top = $r.Top; right = $r.Right; bottom = $r.Bottom; width = ($r.Right - $r.Left); height = ($r.Bottom - $r.Top) }
  }
}

try {
  switch ($Action) {
    "enum-windows" {
      $items = @()
      foreach ($hwnd in [QioNative]::WindowsOf([uint32]$TargetPid)) {
        $items += (Get-WindowInfo ([long]$hwnd))
      }
      $out = [pscustomobject]@{ ok = $true; windows = $items }
    }
    "window" {
      $items = @()
      foreach ($hwnd in [QioNative]::WindowsOf([uint32]$TargetPid)) {
        $info = Get-WindowInfo ([long]$hwnd)
        if ($info.class -ne "IME" -and ($info.title -ne "" -or $info.visible)) { $items += $info }
      }
      $main = $items | Where-Object { $_.title -eq "QIO" } | Select-Object -First 1
      $out = [pscustomobject]@{ ok = $true; main = $main; windows = $items }
    }
    "visible" {
      $items = @()
      foreach ($hwnd in [QioNative]::WindowsOf([uint32]$TargetPid)) {
        $info = Get-WindowInfo ([long]$hwnd)
        if ($info.title -eq "QIO") { $items += $info }
      }
      $main = $items | Select-Object -First 1
      $out = [pscustomobject]@{ ok = $true; visible = [bool]($main -and $main.visible); main = $main }
    }
    "close" {
      $ok = [QioNative]::PostMessageW([IntPtr]$Hwnd, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)  # WM_CLOSE
      $out = [pscustomobject]@{ ok = $ok; hwnd = $Hwnd }
    }
    "post" {
      $ok = [QioNative]::PostMessageW([IntPtr]$Hwnd, [uint32]$Message, [IntPtr]$WParam, [IntPtr]$LParam)
      $out = [pscustomobject]@{ ok = $ok; hwnd = $Hwnd; message = $Message }
    }
    "suspend" {
      $handle = [QioNative]::OpenProcess(0x1F0FFF, $false, [uint32]$TargetPid)
      if ($handle -eq [IntPtr]::Zero) { throw "OpenProcess failed for pid $TargetPid" }
      $status = [QioNative]::NtSuspendProcess($handle)
      [QioNative]::CloseHandle($handle) | Out-Null
      $out = [pscustomobject]@{ ok = ($status -eq 0); pid = $TargetPid; status = $status }
    }
    "screenshot" {
      $h = [IntPtr]$Hwnd
      [QioNative]::ShowWindow($h, 9) | Out-Null   # SW_RESTORE（只为截图，不用于恢复断言）
      [QioNative]::SetForegroundWindow($h) | Out-Null
      Start-Sleep -Milliseconds 500
      $r = New-Object QioNative+RECT
      [QioNative]::GetWindowRect($h, [ref]$r) | Out-Null
      $w = $r.Right - $r.Left; $hh = $r.Bottom - $r.Top
      if ($w -le 0 -or $hh -le 0) { throw "window rect invalid: $w x $hh" }
      Add-Type -AssemblyName System.Drawing
      $bmp = New-Object System.Drawing.Bitmap($w, $hh)
      $g = [System.Drawing.Graphics]::FromImage($bmp)
      $g.CopyFromScreen($r.Left, $r.Top, 0, 0, $bmp.Size)
      $bmp.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
      $g.Dispose(); $bmp.Dispose()
      $out = [pscustomobject]@{ ok = $true; path = $Path; width = $w; height = $hh; rect = @{ left = $r.Left; top = $r.Top } }
    }
    "kill-tree" {
      $p = Start-Process -FilePath "taskkill.exe" -ArgumentList @("/PID", "$TargetPid", "/T", "/F") -NoNewWindow -Wait -PassThru
      $out = [pscustomobject]@{ ok = ($p.ExitCode -eq 0 -or $p.ExitCode -eq 128); pid = $TargetPid; exit = $p.ExitCode }
    }
    "tray-click" {
      # tray-icon crate：隐藏窗口类名 tray_icon_app，回调消息 WM_USER_TRAYICON=6002，
      # lParam = Win32 鼠标消息（WM_LBUTTONUP=0x0202）。左键抬起 → Tauri 的 show_main_window。
      $tray = $null
      foreach ($hwnd in [QioNative]::WindowsOf([uint32]$TargetPid)) {
        if ([QioNative]::ClassOf($hwnd) -eq "tray_icon_app") { $tray = $hwnd; break }
      }
      if ($null -eq $tray) { $out = [pscustomobject]@{ ok = $false; error = "tray_icon_app window not found"; pid = $TargetPid } }
      else {
        $ok = [QioNative]::PostMessageW($tray, 6002, [IntPtr]::Zero, [IntPtr]0x0202)
        $out = [pscustomobject]@{ ok = $ok; hwnd = [long]$tray; message = 6002; lparam = 514 }
      }
    }
    "tray-right-click" {
      $tray = $null
      foreach ($hwnd in [QioNative]::WindowsOf([uint32]$TargetPid)) {
        if ([QioNative]::ClassOf($hwnd) -eq "tray_icon_app") { $tray = $hwnd; break }
      }
      if ($null -eq $tray) { $out = [pscustomobject]@{ ok = $false; error = "tray_icon_app window not found" } }
      else {
        # 右键抬起：tray-icon 会 show_menu（TrackPopupMenu），菜单项由「显示主窗口 / 退出 QIO」组成。
        $ok = [QioNative]::PostMessageW($tray, 6002, [IntPtr]::Zero, [IntPtr]0x0205)
        $out = [pscustomobject]@{ ok = $ok; hwnd = [long]$tray; lparam = 517 }
      }
    }
    "uia-invoke" {
      # 用 UI Automation 在已打开的托盘原生菜单里按名字点击菜单项。
      Add-Type -AssemblyName UIAutomationClient
      Add-Type -AssemblyName UIAutomationTypes
      $root = [System.Windows.Automation.AutomationElement]::RootElement
      $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, $Name)
      $deadline = (Get-Date).AddSeconds(8)
      $element = $null
      while ((Get-Date) -lt $deadline) {
        $element = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
        if ($element) { break }
        Start-Sleep -Milliseconds 250
      }
      if (-not $element) { $out = [pscustomobject]@{ ok = $false; error = "UIA element not found: $Name" } }
      else {
        $pattern = $element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $pattern.Invoke()
        $out = [pscustomobject]@{ ok = $true; name = $Name; controlType = $element.Current.ControlType.ProgrammaticName }
      }
    }
    "windows-all" {
      $items = @()
      foreach ($hwnd in [QioNative]::WindowsOf([uint32]$TargetPid)) {
        $items += (Get-WindowInfo ([long]$hwnd))
      }
      $out = [pscustomobject]@{ ok = $true; windows = $items }
    }
    "menu-keys" {
      # 托盘菜单是原生 popup（类名 #32768），属于应用线程。向它 PostMessage 方向键 + 回车：
      # Down 一次=高亮第一项（显示主窗口），两次=第二项（退出 QIO）。
      $menu = $null
      foreach ($hwnd in [QioNative]::WindowsOf([uint32]$TargetPid)) {
        if ([QioNative]::ClassOf($hwnd) -eq "#32768") { $menu = $hwnd; break }
      }
      if ($null -eq $menu) { $out = [pscustomobject]@{ ok = $false; error = "native menu window (#32768) not found" } }
      else {
        $keys = @(0x28, 0x28, 0x0D)   # VK_DOWN, VK_DOWN, VK_RETURN
        foreach ($vk in $keys) {
          [QioNative]::PostMessageW($menu, 0x0100, [IntPtr]$vk, [IntPtr]0) | Out-Null  # WM_KEYDOWN
          [QioNative]::PostMessageW($menu, 0x0101, [IntPtr]$vk, [IntPtr]0) | Out-Null  # WM_KEYUP
          Start-Sleep -Milliseconds 250
        }
        $out = [pscustomobject]@{ ok = $true; hwnd = [long]$menu; keys = "down,down,enter" }
      }
    }
    "uia-dump" {
      Add-Type -AssemblyName UIAutomationClient
      Add-Type -AssemblyName UIAutomationTypes
      $root = [System.Windows.Automation.AutomationElement]::RootElement
      $all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
      $names = @()
      foreach ($el in $all) {
        try {
          $name = $el.Current.Name
          $type = $el.Current.ControlType.ProgrammaticName
          if ($name -and ($type -match "Menu|Button|Popup")) { $names += ($name + " [" + $type + "]") }
        } catch { }
        if ($names.Count -ge 40) { break }
      }
      $out = [pscustomobject]@{ ok = $true; found = $names }
    }
    "processes" {
      $all = Get-CimInstance Win32_Process | Where-Object { $_.Name -match "^(qio|python|pythonw|uvicorn|qio-backend)" -or ($_.CommandLine -and $_.CommandLine -like "*agent.main*") }
      $out = [pscustomobject]@{
        ok = $true
        processes = @($all | ForEach-Object {
          [pscustomobject]@{ pid = $_.ProcessId; ppid = $_.ParentProcessId; name = $_.Name; cmd = $_.CommandLine }
        })
      }
    }
    default { throw "unknown action: $Action" }
  }
  $out | ConvertTo-Json -Depth 8 -Compress
} catch {
  [pscustomobject]@{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress
  exit 2
}