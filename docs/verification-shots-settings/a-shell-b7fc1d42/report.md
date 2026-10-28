# 设置页视觉核查报告（a-shell-b7fc1d42-final）

- 应用：http://127.0.0.1:5107
- 尺寸：1440x900 / 1024x768 / 800x600 / 480x600
- 主题：dark
- 截图：46 张（同目录）
- 检查：104 条，通过 100，失败 4


| 范围 | 检查 | 结果 | 证据摘要 |
| --- | --- | --- | --- |
| dark-1440x900 | settings-root-present | 通过 | {"x":0,"y":32,"width":1440,"height":868} |
| dark-1440x900 | theme-applied | 通过 | {"expected":"dark","actual":"dark"} |
| dark-1440x900 | radius-token-is-14px | 通过 | "14px" |
| dark-1440x900 | global-bar-single-and-fixed | 通过 | {"chromeCount":1,"chromeInsideRoot":0,"chrome":{"rect":{"x":0,"y":0,"width":1440,"height":32,"bottom":32},"position":"fixed","zIndex":"240"},"root":{"x":0,"y":32,"width":1440,"height":868,"top":32},"overlap":false,"fixed |
| dark-1440x900 | global-bar-not-replaced | 通过 | [] |
| dark-1440x900 | global-bar-no-overlap | 通过 | {"rootTop":32,"chromeBottom":32} |
| dark-1440x900 | five-categories | 通过 | {"labels":["常规","模型与服务","记忆与整理","工具与权限","关于与更新"],"missing":[],"unexpected":[],"count":5} |
| dark-1440x900 | advanced-regular-expanded | 通过 | {"opened":1} |
| dark-1440x900 | advanced-models-expanded | 通过 | {"opened":1} |
| dark-1440x900 | advanced-memory-expanded | 通过 | {"opened":1} |
| dark-1440x900 | no-horizontal-scroll | 通过 | [] |
| dark-1440x900 | no-native-select-number-checkbox | 通过 | [] |
| dark-1440x900 | radius-uniform-token | **失败** | [{"cat":"tools","radius":"10px 10px 10px 10px","w":1192,"h":67,"el":{"tag":"button","cls":"mode-option","id":"","role":"radio","aria":"","type":"button","text":"只读模式QIO可以读取文件；不能修改、创建或删除你的业务"}},{"cat":"tools","radius":"10 |
| dark-1440x900 | radius-edge-attached-reported | 通过 | [] |
| dark-1440x900 | nav-layout-side | 通过 | {"expected":"side","modes":["side"]} |
| dark-1440x900 | single-vertical-scroll | 通过 | {"main":null,"nested":[],"bounded":[],"document":{"scrollHeight":900,"clientHeight":900,"scrollWidth":1440,"clientWidth":1440,"horizontalOk":true},"rootOverflowY":"auto"} |
| dark-1440x900 | no-duplicate-heading-desc | 通过 | [] |
| dark-1440x900 | no-native-dialogs | 通过 | {"confirm":0,"alert":0,"prompt":0,"samples":[]} |
| dark-1440x900 | search-results | 通过 | {"count":1,"sample":[{"text":"默认返回条数模型与服务每次搜索返回的结果数（1-20）","rect":{"x":226,"y":160.78125,"width":1192,"height":55,"right":1418,"bottom":215.78125,"top":160.78125,"left":226},"el":{"tag":"button","cls":"set-search__hit"," |
| dark-1440x900 | search-locate-highlight | 通过 | {"before":"模型与服务","after":"模型与服务","highlighted":[{"tag":"div","cls":"set-row.is-revealed","id":"","role":"","aria":"","type":"","text":"默认返回条数每次搜索返回的结果数（1-20）"}],"clicked":{"ok":true,"clicked":{"tag":"button","cls":"set- |
| dark-1440x900 | long-text-readable | 通过 | {"expanded":2,"textLength":20000} |
| dark-1440x900 | single-vertical-scroll-under-overflow | 通过 | {"main":{"el":{"tag":"div","cls":"qio-settings.qio-scroll-host","id":"qio-scroll-3","role":"","aria":"","type":"","text":"返回搜索结果常规模型与服务记忆与整理工具与权限关于与更新"},"scrollHeight":68002,"clientHeight":868,"overflowY":"auto"},"nested |
| dark-1440x900 | no-runtime-errors | 通过 | {"consoleErrors":[],"httpFails":[]} |
| dark-1440x900 | keyboard-reaches-controls | 通过 | {"distinct":8,"navReached":true,"inputReached":true,"missingIndicator":[]} |
| dark-1440x900 | keyboard-focus-visible | 通过 | {"missing":0,"total":8,"samples":[]} |
| reduced-motion-dark-1440x900 | motion-attr-reduced | 通过 | "reduced" |
| reduced-motion-dark-1440x900 | reduced-motion-no-displacement-animation | 通过 | [] |
| dark-1024x768 | settings-root-present | 通过 | {"x":0,"y":32,"width":1024,"height":736} |
| dark-1024x768 | theme-applied | 通过 | {"expected":"dark","actual":"dark"} |
| dark-1024x768 | radius-token-is-14px | 通过 | "14px" |
| dark-1024x768 | global-bar-single-and-fixed | 通过 | {"chromeCount":1,"chromeInsideRoot":0,"chrome":{"rect":{"x":0,"y":0,"width":1024,"height":32,"bottom":32},"position":"fixed","zIndex":"240"},"root":{"x":0,"y":32,"width":1024,"height":736,"top":32},"overlap":false,"fixed |
| dark-1024x768 | global-bar-not-replaced | 通过 | [] |
| dark-1024x768 | global-bar-no-overlap | 通过 | {"rootTop":32,"chromeBottom":32} |
| dark-1024x768 | five-categories | 通过 | {"labels":["常规","模型与服务","记忆与整理","工具与权限","关于与更新"],"missing":[],"unexpected":[],"count":5} |
| dark-1024x768 | advanced-regular-expanded | 通过 | {"opened":1} |
| dark-1024x768 | advanced-models-expanded | 通过 | {"opened":1} |
| dark-1024x768 | advanced-memory-expanded | 通过 | {"opened":1} |
| dark-1024x768 | no-horizontal-scroll | 通过 | [] |
| dark-1024x768 | no-native-select-number-checkbox | 通过 | [] |
| dark-1024x768 | radius-uniform-token | **失败** | [{"cat":"tools","radius":"10px 10px 10px 10px","w":776,"h":86,"el":{"tag":"button","cls":"mode-option","id":"","role":"radio","aria":"","type":"button","text":"只读模式QIO可以读取文件；不能修改、创建或删除你的业务"}},{"cat":"tools","radius":"10p |
| dark-1024x768 | radius-edge-attached-reported | 通过 | [] |
| dark-1024x768 | nav-layout-side | 通过 | {"expected":"side","modes":["side"]} |
| dark-1024x768 | single-vertical-scroll | 通过 | {"main":{"el":{"tag":"div","cls":"qio-settings.qio-scroll-host","id":"qio-scroll-3","role":"","aria":"","type":"","text":"常规模型与服务记忆与整理工具与权限关于与更新版本模型Ke"},"scrollHeight":812,"clientHeight":736,"overflowY":"auto"},"nested": |
| dark-1024x768 | no-duplicate-heading-desc | 通过 | [] |
| dark-1024x768 | no-native-dialogs | 通过 | {"confirm":0,"alert":0,"prompt":0,"samples":[]} |
| dark-1024x768 | search-results | 通过 | {"count":1,"sample":[{"text":"默认返回条数模型与服务每次搜索返回的结果数（1-20）","rect":{"x":226,"y":160.78125,"width":776,"height":55,"right":1002,"bottom":215.78125,"top":160.78125,"left":226},"el":{"tag":"button","cls":"set-search__hit","i |
| dark-1024x768 | search-locate-highlight | 通过 | {"before":"模型与服务","after":"模型与服务","highlighted":[{"tag":"div","cls":"set-row.is-revealed","id":"","role":"","aria":"","type":"","text":"默认返回条数每次搜索返回的结果数（1-20）"}],"clicked":{"ok":true,"clicked":{"tag":"button","cls":"set- |
| dark-1024x768 | long-text-readable | 通过 | {"expanded":2,"textLength":20000} |
| dark-1024x768 | single-vertical-scroll-under-overflow | 通过 | {"main":{"el":{"tag":"div","cls":"qio-settings.qio-scroll-host","id":"qio-scroll-3","role":"","aria":"","type":"","text":"返回搜索结果常规模型与服务记忆与整理工具与权限关于与更新"},"scrollHeight":78982,"clientHeight":736,"overflowY":"auto"},"nested |
| dark-1024x768 | no-runtime-errors | 通过 | {"consoleErrors":[],"httpFails":[]} |
| dark-800x600 | settings-root-present | 通过 | {"x":0,"y":32,"width":800,"height":568} |
| dark-800x600 | theme-applied | 通过 | {"expected":"dark","actual":"dark"} |
| dark-800x600 | radius-token-is-14px | 通过 | "14px" |
| dark-800x600 | global-bar-single-and-fixed | 通过 | {"chromeCount":1,"chromeInsideRoot":0,"chrome":{"rect":{"x":0,"y":0,"width":800,"height":32,"bottom":32},"position":"fixed","zIndex":"240"},"root":{"x":0,"y":32,"width":800,"height":568,"top":32},"overlap":false,"fixedIn |
| dark-800x600 | global-bar-not-replaced | 通过 | [] |
| dark-800x600 | global-bar-no-overlap | 通过 | {"rootTop":32,"chromeBottom":32} |
| dark-800x600 | five-categories | 通过 | {"labels":["常规","模型与服务","记忆与整理","工具与权限","关于与更新"],"missing":[],"unexpected":[],"count":5} |
| dark-800x600 | advanced-regular-expanded | 通过 | {"opened":1} |
| dark-800x600 | advanced-models-expanded | 通过 | {"opened":1} |
| dark-800x600 | advanced-memory-expanded | 通过 | {"opened":1} |
| dark-800x600 | no-horizontal-scroll | 通过 | [] |
| dark-800x600 | no-native-select-number-checkbox | 通过 | [] |
| dark-800x600 | radius-uniform-token | **失败** | [{"cat":"tools","radius":"10px 10px 10px 10px","w":756,"h":86,"el":{"tag":"button","cls":"mode-option","id":"","role":"radio","aria":"","type":"button","text":"只读模式QIO可以读取文件；不能修改、创建或删除你的业务"}},{"cat":"tools","radius":"10p |
| dark-800x600 | radius-edge-attached-reported | 通过 | [] |
| dark-800x600 | nav-layout-top | 通过 | {"expected":"top","modes":["top"]} |
| dark-800x600 | single-vertical-scroll | 通过 | {"main":{"el":{"tag":"div","cls":"qio-settings.qio-scroll-host","id":"qio-scroll-3","role":"","aria":"","type":"","text":"常规模型与服务记忆与整理工具与权限关于与更新版本模型Ke"},"scrollHeight":1011,"clientHeight":568,"overflowY":"auto"},"nested" |
| dark-800x600 | no-duplicate-heading-desc | 通过 | [] |
| dark-800x600 | no-native-dialogs | 通过 | {"confirm":0,"alert":0,"prompt":0,"samples":[]} |
| dark-800x600 | search-results | 通过 | {"count":1,"sample":[{"text":"默认返回条数模型与服务每次搜索返回的结果数（1-20）","rect":{"x":22,"y":251.78125,"width":280,"height":55,"right":302,"bottom":306.78125,"top":251.78125,"left":22},"el":{"tag":"button","cls":"set-search__hit","id": |
| dark-800x600 | search-locate-highlight | 通过 | {"before":"模型与服务","after":"模型与服务","highlighted":[{"tag":"div","cls":"set-row.is-revealed","id":"","role":"","aria":"","type":"","text":"默认返回条数每次搜索返回的结果数（1-20）"}],"clicked":{"ok":true,"clicked":{"tag":"button","cls":"set- |
| dark-800x600 | long-text-readable | 通过 | {"expanded":2,"textLength":20000} |
| dark-800x600 | single-vertical-scroll-under-overflow | 通过 | {"main":{"el":{"tag":"div","cls":"qio-settings.qio-scroll-host","id":"qio-scroll-3","role":"","aria":"","type":"","text":"返回搜索结果常规模型与服务记忆与整理工具与权限关于与更新"},"scrollHeight":80339,"clientHeight":568,"overflowY":"auto"},"nested |
| dark-800x600 | no-runtime-errors | 通过 | {"consoleErrors":[],"httpFails":[]} |
| dark-480x600 | settings-root-present | 通过 | {"x":0,"y":32,"width":480,"height":568} |
| dark-480x600 | theme-applied | 通过 | {"expected":"dark","actual":"dark"} |
| dark-480x600 | radius-token-is-14px | 通过 | "14px" |
| dark-480x600 | global-bar-single-and-fixed | 通过 | {"chromeCount":1,"chromeInsideRoot":0,"chrome":{"rect":{"x":0,"y":0,"width":480,"height":32,"bottom":32},"position":"fixed","zIndex":"240"},"root":{"x":0,"y":32,"width":480,"height":568,"top":32},"overlap":false,"fixedIn |
| dark-480x600 | global-bar-not-replaced | 通过 | [] |
| dark-480x600 | global-bar-no-overlap | 通过 | {"rootTop":32,"chromeBottom":32} |
| dark-480x600 | five-categories | 通过 | {"labels":["常规","模型与服务","记忆与整理","工具与权限","关于与更新"],"missing":[],"unexpected":[],"count":5} |
| dark-480x600 | advanced-regular-expanded | 通过 | {"opened":1} |
| dark-480x600 | advanced-models-expanded | 通过 | {"opened":1} |
| dark-480x600 | advanced-memory-expanded | 通过 | {"opened":1} |
| dark-480x600 | no-horizontal-scroll | 通过 | [] |
| dark-480x600 | no-native-select-number-checkbox | 通过 | [] |
| dark-480x600 | radius-uniform-token | **失败** | [{"cat":"tools","radius":"10px 10px 10px 10px","w":436,"h":106,"el":{"tag":"button","cls":"mode-option","id":"","role":"radio","aria":"","type":"button","text":"只读模式QIO可以读取文件；不能修改、创建或删除你的业务"}},{"cat":"tools","radius":"10 |
| dark-480x600 | radius-edge-attached-reported | 通过 | [] |
| dark-480x600 | nav-layout-top | 通过 | {"expected":"top","modes":["top"]} |
| dark-480x600 | single-vertical-scroll | 通过 | {"main":{"el":{"tag":"div","cls":"qio-settings.qio-scroll-host","id":"qio-scroll-3","role":"","aria":"","type":"","text":"常规模型与服务记忆与整理工具与权限关于与更新版本模型Ke"},"scrollHeight":1140,"clientHeight":568,"overflowY":"auto"},"nested" |
| dark-480x600 | no-duplicate-heading-desc | 通过 | [] |
| dark-480x600 | no-native-dialogs | 通过 | {"confirm":0,"alert":0,"prompt":0,"samples":[]} |
| dark-480x600 | search-results | 通过 | {"count":1,"sample":[{"text":"默认返回条数模型与服务每次搜索返回的结果数（1-20）","rect":{"x":22,"y":292.78125,"width":280,"height":55,"right":302,"bottom":347.78125,"top":292.78125,"left":22},"el":{"tag":"button","cls":"set-search__hit","id": |
| dark-480x600 | search-locate-highlight | 通过 | {"before":"模型与服务","after":"模型与服务","highlighted":[{"tag":"div","cls":"set-row.is-revealed","id":"","role":"","aria":"","type":"","text":"默认返回条数每次搜索返回的结果数（1-20）"}],"clicked":{"ok":true,"clicked":{"tag":"button","cls":"set- |
| dark-480x600 | long-text-readable | 通过 | {"expanded":2,"textLength":20000} |
| dark-480x600 | single-vertical-scroll-under-overflow | 通过 | {"main":{"el":{"tag":"div","cls":"qio-settings.qio-scroll-host","id":"qio-scroll-3","role":"","aria":"","type":"","text":"返回搜索结果常规模型与服务记忆与整理工具与权限关于与更新"},"scrollHeight":129987,"clientHeight":568,"overflowY":"auto"},"neste |
| dark-480x600 | no-runtime-errors | 通过 | {"consoleErrors":[],"httpFails":[]} |
| dark-480x600 | keyboard-reaches-controls | 通过 | {"distinct":8,"navReached":true,"inputReached":true,"missingIndicator":[]} |
| dark-480x600 | keyboard-focus-visible | 通过 | {"missing":0,"total":8,"samples":[]} |
| reduced-motion-dark-480x600 | motion-attr-reduced | 通过 | "reduced" |
| reduced-motion-dark-480x600 | reduced-motion-no-displacement-animation | 通过 | [] |
| structure | same-dom-order-dark-1024x768-vs-1440x900 | 通过 | {"count":38} |
| structure | same-dom-order-dark-800x600-vs-1440x900 | 通过 | {"count":38} |
| structure | same-dom-order-dark-480x600-vs-1440x900 | 通过 | {"count":38} |
| structure | structure-hook-present-dark | 通过 | {"sources":["data-settings-key"]} |

## 跳过（占位 / 依赖未交付，不判红也不计入通过）

| 范围 | 检查 | 原因 |
| --- | --- | --- |
| dark-1440x900 | long-form-opens | 模型与服务里的 Key 面板由 B 提供，当前分支是占位（data-testid=models-panel-placeholder），不判红 |
| dark-1440x900 | confirm-layer | Key 管理面板由 B 提供，当前是占位；危险操作确认层在集成后才有入口，不判红 |
| dark-1024x768 | long-form-opens | 模型与服务里的 Key 面板由 B 提供，当前分支是占位（data-testid=models-panel-placeholder），不判红 |
| dark-1024x768 | confirm-layer | Key 管理面板由 B 提供，当前是占位；危险操作确认层在集成后才有入口，不判红 |
| dark-800x600 | long-form-opens | 模型与服务里的 Key 面板由 B 提供，当前分支是占位（data-testid=models-panel-placeholder），不判红 |
| dark-800x600 | confirm-layer | Key 管理面板由 B 提供，当前是占位；危险操作确认层在集成后才有入口，不判红 |
| dark-480x600 | long-form-opens | 模型与服务里的 Key 面板由 B 提供，当前分支是占位（data-testid=models-panel-placeholder），不判红 |
| dark-480x600 | confirm-layer | Key 管理面板由 B 提供，当前是占位；危险操作确认层在集成后才有入口，不判红 |
