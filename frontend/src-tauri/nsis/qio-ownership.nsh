; 卸载段「主程序还在跑吗」的**所有权**守卫（构建期模板补丁的目标语句）。
;
; 为什么必须有它（2026-10-03 逐行核对生成的模板 + 实测）：
;   Tauri 生成的 installer.nsi 卸载段里有这样一行：
;       !insertmacro CheckIfAppIsRunning "${MAINBINARYNAME}.exe" "${PRODUCTNAME}"
;   而 utils.nsh 里这个宏的实现是 FindProcessCurrentUser / KillProcessCurrentUser —— **只按可执行
;   文件名**判定与结束（插件没有 path 参数，也没有按 pid 的接口）。后果：
;     * 静默卸载 IfSilent 直接跳 kill_ 标签 → 杀掉当前用户**所有** qio.exe；
;     * 交互卸载弹的是同一个确认框，用户点确定 → 同样把所有 qio.exe 杀掉；
;     * 被杀的那个壳持有 Job Object（JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE），它一死 → job 关闭
;       → 它那份安装的 backend 进程树一起死。
;   也就是说「卸载 A 误杀 B 的 sidecar」有一条**绕过 sidecar 所有权**的路径：杀壳 → 关 job → 杀树。
;   只改 sidecar 那一侧不够，这一行也必须换成按**本安装实例的所有权记录**判定。
;
; 语义（与模板原来的分支一一对应，只是把判定换成所有权）：
;   1. 先问帮助程序 --check-only（**只读**、绝不动进程）：本安装实例的壳还在不在？
;   2. 在 → 保持模板行为：交互卸载弹原来的确认框，静默卸载直接收；收的时候调
;      --close-installation --allow-main-exe（按 lease 里的 shell 记录发 WM_CLOSE，超时才
;      taskkill /PID <pid>；绝不按名字）。
;   3. 不在 / 记录对不上 / 帮助程序缺失 / nsExec 起不来 → **什么都不做**，直接往下走。
;      宁可留下删不掉的文件，也不误杀别的安装的进程。
;
; 文案与模板 utils.nsh 用的是同一批语言串（appRunning / appRunningOkKill / failedToKillApp），
; 模板自己也是用 nsis_tauri_utils::StrReplace 做 {{product_name}} 占位替换的。

!macro QIO_CloseMainExeIfOwned
  !define QIO_UID ${__LINE__}
  nsExec::ExecToStack '"$INSTDIR\qio-uninstall-helper.exe" --check-only --install-dir "$INSTDIR" --json'
  Pop $9 ; nsExec 返回（ok / error / timeout）
  Pop $8 ; 退出码（0 = 本安装实例的壳在跑；3 = 无法确认/没在跑）
  Pop $7 ; 一行诊断
  StrCmp $8 "0" 0 qio_main_skip_${QIO_UID}
    IfSilent qio_main_kill_${QIO_UID}
    ${IfThen} $PassiveMode != 1 ${|} MessageBox MB_OKCANCEL $R2 IDOK qio_main_kill_${QIO_UID} IDCANCEL qio_main_cancel_${QIO_UID} ${|}
  qio_main_kill_${QIO_UID}:
    nsExec::ExecToStack '"$INSTDIR\qio-uninstall-helper.exe" --close-installation --allow-main-exe --install-dir "$INSTDIR" --timeout-ms 8000 --json'
    Pop $6
    Pop $5
    Pop $4
    StrCmp $5 "0" 0 qio_main_failed_${QIO_UID}
  qio_main_failed_${QIO_UID}:
    IfSilent qio_main_silent_${QIO_UID} qio_main_ui_${QIO_UID}
  qio_main_silent_${QIO_UID}:
    System::Call 'kernel32::AttachConsole(i -1)i.r0'
    ${If} $0 != 0
      System::Call 'kernel32::GetStdHandle(i -11)i.r0'
      System::call 'kernel32::SetConsoleTextAttribute(i r0, i 0x0004)'
      FileWrite $0 "$R1$\n"
    ${EndIf}
    Abort
  qio_main_ui_${QIO_UID}:
    Abort $R3
  qio_main_cancel_${QIO_UID}:
    Abort $R1
  qio_main_skip_${QIO_UID}:
  !undef QIO_UID
!macroend

!define QIO_NSIS_GUARD_APPLIED 1
