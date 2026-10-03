; QIO 自定义 NSIS 钩子（Tauri v2 bundle.windows.nsis.installerHooks）
;
; ⚠️ 这个文件**必须与构建期的模板补丁一起用**（scripts/build_nsis_with_patch.py）。
; 下面这段是硬门禁：模板卸载段里有一处按**可执行文件名**杀 qio.exe 的检查
; （杀当前用户所有 qio.exe → 壳死 → Job Object 关闭 → 另一份安装的 backend 也死），
; Tauri 的 installerHooks 只能追加宏、不能替换模板里已有的语句，所以补丁必须在
; 「生成 installer.nsi」与「makensis 编译」之间把那一处换成 !insertmacro QIO_CloseMainExeIfOwned，
; 并定义 QIO_OWNERSHIP_PATCHED。
;
; 没打补丁就编译？这里直接 !error 中止 —— 宁可构建失败，也不要出一个"卸载会误杀别人"的安装包。
; 而且这让"补丁到底进没进产物"变成**编译期事实**：构建成功 = 补丁一定生效了。
; （不用事后在产物里找字符串：NSIS 用 LZMA 压整包，字符串搜不到，会得到假阴性。）

!ifndef QIO_OWNERSHIP_PATCHED
  !error "NSIS 模板补丁没打上：installer-hooks.nsh 需要 scripts/build_nsis_with_patch.py 在编译前把模板里按名字杀 qio.exe 的那一处换成所有权守卫。请用该包装脚本构建安装包。"
!endif

; 为什么需要它
; ------------
; Tauri 生成的卸载段只在**用户勾选「删除应用数据」**时才清理 HKCU\Software\qio\QIO
; （生成脚本 Section Uninstall 里的复选框分支）。于是「普通卸载」会留下安装位置记录，
; 而下一次安装的 .onInit 会调用 RestorePreviousInstallLocation 读这个键 ——
; 结果是默认安装目录被指到一个**已经被删掉的路径**。
;
; 安装信息 vs 用户数据（这条边界是这个文件存在的全部理由）
; ------------------------------------------------------
;   * 删：默认值（安装位置，安装器写的）与 "Installer Language"（安装向导语言，模板写的）
;   * 不删：同一个键下的 "DbBaseline"（后端 agent/storage/db_identity.py 写的数据库身份基线，
;     属于**用户状态**，不是安装信息 —— 删了就等于改了产品既有的数据保留策略）
;   * 所以只用 DeleteRegValue + DeleteRegKey /ifempty，**绝不**对整键 DeleteRegKey
;   * 完全不碰数据目录（%APPDATA%\com.qio.app / %LOCALAPPDATA%\com.qio.app）：
;     数据是否保留由模板里那个「删除应用数据」复选框决定，本钩子不参与
;
; 为什么用 POSTUNINSTALL：此时模板已经删掉控制面板登记（UNINSTKEY），
; 我们补上它没管的安装位置记录。SHCTX 与模板一致（currentUser 安装即 HKCU），
; 由 un.onInit 的 SetContext 设好。
;
; 为什么不改整个 NSIS 模板：模板有 900+ 行，是 Tauri 生成的；fork 一份就等于把
; Tauri 的升级全部变成人工合并。钩子是官方支持的扩展点，升级时不会炸。

!macro NSIS_HOOK_POSTUNINSTALL
  ClearErrors
  ; 安装位置（默认值）
  DeleteRegValue SHCTX "Software\qio\QIO" ""
  ; 安装向导语言
  DeleteRegValue SHCTX "Software\qio\QIO" "Installer Language"
  ; 键空了才删：只要 DbBaseline 还在，这两行就是 no-op（用户状态必须留下）
  DeleteRegKey /ifempty SHCTX "Software\qio\QIO"
  DeleteRegKey /ifempty SHCTX "Software\qio"

  ; 安装目录收尾（2026-10-03 实测的卸载残留）
  ; ----------------------------------------
  ; Tauri 的卸载段只删**登记过的文件**（逐个 Delete），最后只做一次非递归的
  ; RMDir "$INSTDIR" —— 只要安装目录里多出任何一个运行期生成的文件，整个目录就删不掉。
  ; 实测：装完用一次依赖型工具后卸载，残留整个 python-runtime（33.9MB）——
  ; venv 与工具用的是安装包自带解释器，stdlib 就在 <安装目录>\python-runtime\Lib 下，
  ; import 时生成的 __pycache__ 就是那个"没登记过的文件"。
  ;
  ; 根治在 backend（tools/tool_envs.py 给 python 子进程钉 PYTHONDONTWRITEBYTECODE=1）；
  ; 这里做兜底：把**属于我们的**资源目录整棵删掉，再重试一次非递归 RMDir。
  ; 只碰本安装目录里的这两处，绝不 RMDir /r "$INSTDIR"（用户可能把 QIO 装在
  ; 别的目录旁边，整棵删会连带删掉不属于我们的东西），也绝不碰数据目录。
  RMDir /r "$INSTDIR\python-runtime"
  ; 外壳异常退出时可能留下的归属记录/诊断文件：不清掉同样会让目录删不掉
  Delete "$INSTDIR\sidecar.lease.json"
  Delete "$INSTDIR\sidecar.lease.json.tmp*"
  Delete "$INSTDIR\qio-startup-error.txt"
  RMDir "$INSTDIR"
!macroend
; ---------------------------------------------------------------------------
; 卸载前：只收「属于这一个安装实例」的后台进程
;
; 以前这里写的是 !insertmacro CheckIfAppIsRunning "qio-backend.exe" —— 它按**可执行
; 文件名**找当前用户的进程，于是同一用户下存在两份 QIO 安装时，卸载 A 会把 B 的 sidecar
; 一起结束（**已确认 Bug**，2026-10-03 用改动前产物实测复现）。按名字只能证明
; 「机器上有个 qio-backend」，不能证明「这个 qio-backend 属于正在卸载的这一份」。
;
; 现在换成按**所有权记录**收（frontend/src-tauri/src/ownership.rs 是唯一实现）：
;   * 外壳启动时在安装目录写 sidecar.lease.json，记下自己与后端的 pid + 进程创建时间
;     （100ns FILETIME）+ 映像路径。PID 复用会被创建时间挡掉。
;   * 这里调同源的 qio-uninstall-helper.exe：记录对得上才动手（先 WM_CLOSE 让外壳自己
;     按 job 收整棵树，超时才 taskkill /PID），对不上就**什么都不动**。
;   * 帮助程序缺失 / 起不来 / 判定不了 → 退出码非 0，直接往下走，
;     **绝不**回退成"按名字杀"。宁可留下删不掉的文件，也不误杀别人的进程。
;
; 壳自己就是 sidecar 的所有者（Windows 下有 Job Object：关句柄即收整棵树），所以这里
; 只是"壳没能自己收干净"时的兜底 —— 不是主路径。
;
; 后面模板自带的 CheckIfAppIsRunning 仍然保留（它查的是主程序 qio.exe）：那是模板行为，
; 本轮不动。上面的帮助程序会先把本实例关掉，正常情况下它不会再命中。
; ---------------------------------------------------------------------------

!macro NSIS_HOOK_PREUNINSTALL
  ; 关掉文件系统重定向，避免 32 位上下文里判定路径出错（与 Tauri 模板同一写法）
  ${DisableX64FSRedirection}
  ClearErrors
  nsExec::ExecToStack '"$INSTDIR\qio-uninstall-helper.exe" --close-installation --install-dir "$INSTDIR" --timeout-ms 8000 --json'
  Pop $0 ; nsExec 的返回（"ok" / "error" / "timeout"）
  Pop $1 ; 退出码
  Pop $2 ; 一行诊断（已脱敏，不含密钥）
  ${EnableX64FSRedirection}
  DetailPrint "sidecar 归属检查：$2（exit=$1）"
!macroend
