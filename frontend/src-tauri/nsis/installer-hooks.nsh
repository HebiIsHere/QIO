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
