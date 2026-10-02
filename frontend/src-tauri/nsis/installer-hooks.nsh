; QIO 自定义 NSIS 钩子（Tauri v2 bundle.windows.nsis.installerHooks）
;
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
!macroend
; ---------------------------------------------------------------------------
; 卸载前：把还在跑的 sidecar 收掉
;
; 为什么需要它（2026-10-03 两条独立实测拼起来的事实）：
;   * 运行中的 qio-backend.exe **删不掉也覆盖不了**（delete -> WinError 5，
;     overwrite -> Errno 13），只有 rename 能成功（映像以 FILE_SHARE_DELETE 打开）
;     —— Agent A 用冻结产物实测（scripts/verify_backend_process_model.py --case 6）；
;   * onefile 是 launcher + child 两层，只结束 launcher 会留下孤儿 child，
;     它继续持有映像、端口也仍然开着（我自己的安装/卸载 E2E 实测过同样的形状）；
;   * 而 Tauri 模板的 CheckIfAppIsRunning 只查主程序 qio.exe，**不查 sidecar** ——
;     于是 sidecar 还活着时：Delete "$INSTDIR\qio-backend.exe" 静默失败、
;     RMDir "$INSTDIR" 也失败，用户看到的是"卸载完了但目录还在、后端还在跑"。
;
; 这里复用模板自带的同一个宏（utils.nsh 里的 CheckIfAppIsRunning）把 sidecar 纳入检查：
; 静默卸载直接结束它，交互卸载问用户 —— 与主程序的处理方式保持一致。
; 注意：与模板一样是按**可执行文件名**找当前用户的进程；多份 QIO 安装并存时，
; 卸载其中一份会连带结束另一份的 sidecar。这与模板对 qio.exe 的既有行为一致，
; 属于已知限制（见 docs/e2e-install-2026-10-02.md 的"当前限制"）。
; ---------------------------------------------------------------------------

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro CheckIfAppIsRunning "qio-backend.exe" "${PRODUCTNAME}"
!macroend
