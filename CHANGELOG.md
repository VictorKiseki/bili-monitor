# 更新日志

本文件格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

版本号的唯一来源是仓库根目录的 `VERSION` 文件；发版命令见
`docs/31-发布与交接手册.md`（`python scripts/release.py bump|check|build`）。

## 版本号规则

从 `0.1.0` 起按下面三条走（`scripts/release.py` 会按规则校验，第一位必须人工放行）：

| 位置 | 谁改 | 什么时候递增 |
|---|---|---|
| 第一位 `major` | **只能人工** | 整个项目架构级调整，或新增功能模块 |
| 第二位 `minor` | 自动 | 多个重要功能统一合并迭代时 |
| 第三位 `patch` | 自动 | 缺陷修复、细节优化，以及尚未合并的小改动 |

**低位归零**：涨哪一位，右边的位全部清零 —— `0.1.1` 升第二位是 `0.2.0`（不是 `0.2.1`），
`0.9.4` 升第一位是 `1.0.0`；只有 `patch` 在原位加一（`0.2.0` → `0.2.1`）。

命令对应：`release.py bump patch` / `bump minor` 自动递增；`bump major` 必须显式加
`--allow-major`，防止架构级数字被顺手改掉。每个版本的改动都要在下面留一节文字记录
（新增 / 变更 / 修复 / 安全 / 升级提示），发版时不准只改版本号。

## [Unreleased]

## [0.5.0] - 2026-11-21 — GitHub 发布与在线增量更新

### 新增

- Windows 管理台支持在线签名增量更新、新版启动检查和失败回滚。
- 工作台进程页支持设置监控台主进程的 Windows 登录自启。

### 变更

- 监控台和监控室统一使用 GitHub 最新正式发行版检查更新。在线增量包从该版本的 GitHub 附件下载。

### 修复

- 采集状态、事件、通知入队和相关已见标记在同一 SQLite 事务内提交，失败后可重试。
- QQ 换绑保留旧目标历史并取消未完成投递。总开关和目标停用可暂停派发，推送进程自行回收超时任务。
- 临时网络故障保留采集登录态并缩短校验间隔。代理连接池构建失败会回滚和重试。
- Gitee 旧发版脚本补齐建发行版字段，续跑核对远端标签和快照，只补发行版与附件。

### 升级提示

- 从旧版本升级时首次安装支持 GitHub 在线更新的完整发行包。后续代码增量更新需要同一签名公钥和匹配的发行附件。

## [0.4.1] - 2026-11-20 — 设置改完立刻生效：代理热更新与登录态误报

这一版是「改完设置立刻生效」：在页面上换过代理后，采集、推送与监控室会自己换用新连接，不用再手动重启；监控室也不会再把「网络一时连不上」报成「登录已失效」；发版从十来步手工命令收成一条命令。离线用例 738 例通过（1 例按环境跳过）。

### 新增

- **一条命令发版 `scripts/release_gitee.py`**（2026-11-16）：发布要手工走「改版本号 → 提交 → 打 tag → 临时索引做发布线快照 → 打包 → 推三条 ref → 网页建发行版 → 传两个附件」十来步，同一步漏一次就得重来，0.4.0 那次是照着 [docs/34](docs/34-开源发布清单.md) §9 的命令块一步步粘的。现在由脚本按固定顺序串起来：前置检查（`VERSION` 合法、`CHANGELOG` 有这一版小节且占位行都已填写、工作树只脏了 `VERSION`/`CHANGELOG.md`、tag 没被占用）→ 全量测试 → `release.py check` → 提交 `chore(release): vX.Y.Z` → annotated tag → `opensource` 快照（临时索引，剔除 `tests/` 与 `scripts/selftest_*.py`）→ `release.py build` 并核对 `dist/SHA256SUMS` → 推送 与 Gitee 发行版。**中途失败可以修完直接重跑**：已提交的不再提交、tag 已指向 HEAD 就跳过、发行版按附件名字跳过已上传的文件；令牌按 `--token-file` > `GITEE_TOKEN` > `~/.dsh/secrets/gitee.env` 查找且任何输出都不打印（那个文件的真实形状是「说明文字 "<token>"」的单行，不是 `KEY=VALUE`）。`--dry-run` 只读、`--no-release`/`--no-push` 收窄范围、`--yes` 免交互。测试 `tests/test_release_gitee.py`（令牌解析、仓库名解析、CHANGELOG 小节与标题抽取、骨架拒绝、附件名去重、multipart 编码）；文档见 [docs/31](docs/31-发布与交接手册.md) §11「一条命令发版」。

### 修复

- **登录态校验把「网络不通」当成「登录已失效」**（2026-11-20）：扫码登录其实已经成功，`config/livedeck.env` 里 SESSDATA / bili_jct / DedeUserID 都在，顶栏却一直挂着「登录已失效」。两个原因叠加：① 监控室进程从一个带旧代理的父进程继承环境（代理软件已经退出，`HTTPS_PROXY` 还指着 `127.0.0.1:22222`），httpx 0.27 在 `Client()` 构造时就把代理固化进 transport，三个连接池一起往死端口上撞（`[WinError 10061] 由于目标计算机积极拒绝，无法连接。`），登录态校验必然抛异常；② `deck_login` 把任何异常都写成「校验登录态失败」，再按 `CHECK_TTL=300` 节流五分钟，页面就只能看到「已失效」。现在：`LiveDeck.__init__` 先按 `config/proxy.json` 调一次 `proxy_cfg.apply_to_env()` 再建连接池；校验失败先重建一次连接池（有 `apply_proxy()` 就用它，没有就只刷新环境变量）再试一次；连不上记 `state="network"`、被 412 拦记 `"risk"`，只节流 15 秒，芯片文案是「登录态未校验 · 稍后重试」，这两种状态下 `can_send` 仍为 true（Cookie 没坏，真发一次才知道行不行）；只有 B 站明确回 `isLogin=false` 才判 `expired` 并提示重新扫码。`login_snapshot()` 新增 `state` / `error`，`login_poll` / `login_paste` 带回 `warn` 让前端提示「已保存但没校验成功」，顺带 `_remember_identity()` 把 nav 里的 uname / mid 回写进凭据（之前粘贴登录时这两项会丢）。测试 `tests/test_livedeck_login_state.py`（9 例：有效、失效、两次网络错误、先败后成、412、未登录、失败后节流、粘贴的两种状态）。实测：故意带死代理 `127.0.0.1:22222` 启动，20 秒后 `GET /api/snapshot` 就是 `state=ok`、`verified=true`，日志「登录态有效：奇跡のかけら（mid 274172）」。详见 [docs/23](docs/23-多屏直播监控台.md) §3。

- **代理设置保存后，已经在跑的采集 / 推送 / 监控室不跟着换**（2026-11-20）：原来「保存」只对保存的那个进程调 `apply_to_env()`，采集 / 推送 / 监控室是启动时读一次环境变量，页面上那句提示就是「要重启才整体生效」。实际后果比提示难看得多：系统里的代理软件崩溃后没再起来（`config/proxy.json` 的 `ProxyServer` 还指着 `127.0.0.1:22222`，端口已经没人监听），用户在页面上切成「不使用代理」后进程照样往死端口上撞，`logs/collector-*.log` 里是一串 `[WinError 10061] 由于目标计算机积极拒绝，无法连接。`（直连 `api.bilibili.com` 不会返回 10061，只有连本机关闭的端口才会），手动重启进程后才恢复。现在改成三处各自跟上：
  - `shared/proxy.py` 新增 `revision()`（`config/proxy.json` 的 `mtime_ns` + 文件大小指纹）；`web/stack.py` 的 `StackManager` 在每轮对账（默认 2 秒）开头调 `_proxy_reload()`，指纹变了就先 `apply_to_env()` 再**重启在跑的采集 / 推送子进程**（新子进程继承新环境），日志写 `[栈] 代理设置已变更，重启 采集 / 推送 让新配置生效`；没在跑的不动，下次启动自然生效。
  - `livedeck/app.py` 新增 `apply_proxy()`：指纹变了就重建 `stream_http` / `http` / `BiliClient` 三个连接池并关掉旧的，不重启监控室、不断页面与舞台（旧直播流会报错并由前端自动重连）；新增 `deck-proxy` 线程每 5 秒比对一次，**在监控台（8765）那边改的也能跟上**。监控室自己保存时也立即调一次。
  - `scripts/schedule_worker.py` 启动时补 `apply_to_env()`（它也要联网抓周表图 / 刷直播预约）；`core/weekly/fetch.py` 的模块级连接池 `_http()` 按同一指纹重建。
  - 两侧 `/api/proxy` 的 `note` 与弹窗提示改成「本进程立刻换用；采集 / 推送会在几秒内自动重启」，不再叫用户去「进程」页手动重启。监控室静态资源版本串递增到 `20261120a`。测试：`tests/test_proxy_settings.py` 加 `test_revision_tracks_file_changes`、`test_running_processes_pick_up_changes`（并扩到 `scripts/schedule_worker.py`）、`tests/test_stack.py` 新增 `ProxyReloadTests` 3 例（变指纹重启两个子进程 / 同指纹不动 / 没子进程只换环境），离线 738 例通过（1 例按环境跳过）。详见 [docs/12](docs/12-Web管理台.md) §22.13、[docs/23](docs/23-多屏直播监控台.md) §2。

## [0.4.0] - 2026-11-14 — 安全边界与操作手感：第三方审查修复合集

这一版是两份第三方审查报告（产品体验 / 静态代码）的修复合集，三批共 32 项：安全边界（监控室只认环回、FLV 直链与 QQ 网关补白名单、卡片图下载设上限、敏感信息脱敏）、可靠性（重复投递、水位与已见标记时序、退避与风控判定、日志扫描预算），以及监控台与监控室的操作手感（高风险动作二次确认、失败投递可忽略、事件总数口径、日志不再被拽回底部、在播状态统一）。报告里按「本机单机、只监听 127.0.0.1」模型不成立的结论没有照做，理由写在对应条目里。离线用例 699 例通过。

### 变更

- **四个高风险动作补二次确认**（2026-11-14，审查 A10）：顶栏「全停」、停用推送目标、暂停任务、死信重投原先点一下就生效（浏览器实测里「全停」被误点过，只能靠人回忆去恢复）。现在这四处都先 `confirm`，文案写明后果（「进程还在、守护不会重启，恢复需要手动切回来」/「停用后这个目标不再收到任何推送消息」/「暂停后这个任务不会再执行」）。
- **写操作留一行审计日志**（2026-11-14，审查 B-10 / 体验 P2-5）：全库没有任何操作记录——「谁在什么时候把推送目标停了 / 点了全停 / 删了节目」事后只能靠人回忆（浏览器实测里真被误点过一次全停）。现在 `web/console_http.py` 的 `_audit()` 在所有 `POST` / `PATCH` / `DELETE` 成功（<400）后写一行 `[写操作] METHOD /path → 200（12 ms）`，落在监控台自己的 `out/console.log`（监控台以前没配 handler，`log.info` 会被直接丢掉，同一处补了仅在无 handler 时生效的 stdout handler）。**只记方法、路径、状态码、耗时，不记请求体**——body 里有 SESSDATA、openid、代理密码，落日志违反 AGENTS §4；GET 与失败请求不记，免得把日志刷满。测试 `tests/test_review_fixes_2026_11_14_c.py` 的 `AuditTest`（写成功记、GET/400 不记、正文里不出现 `_body()`）。详见 [docs/12](docs/12-Web管理台.md) §2。
- **周表写接口收成一份实现**（2026-11-14，审查细节 1）：`programs` / `manual` / `feature` / `ai` 这四组写接口在 `_post_weekly`、`_handle_patch`、`_handle_delete` 里各抄了一遍，而且三份里都夹着「按别的 method 判断」的分支（`_dispatch` 严格按 method 分派，那些分支永远不会命中）。现在统一走模块级 `_weekly_write(h, method, path, c)`，路由或 method 不匹配就返回 `_UNHANDLED` 交回调用方，404 文案不变；`_handle_patch` 里那个「按 DELETE 判断」的死分支一并删掉（直接跨 method 调用现在按未知接口 404，`tests/test_uid_range.py::test_patch_handler_no_longer_handles_delete` 守着）。顺带 `POST /api/weekly/manual/{id}` 现在能更新（旧实现只有 PATCH 分支会命中）。测试同上文件的 `WeeklyWriteTest`（5 例）。
- **进程页「开机自启」不再整列显示「—」**（2026-11-14，体验 P2-2）：直播 / 动态 / 派发 / 缓存清理 / QQ 收件 / 清理通知 / 日程进程组这些任务本来就是进程内循环，没有独立进程可自启，以前统一显示「—」，看着像「还没做自启」。现在按所属进程给文案：`随采集进程运行` / `随推送进程运行` / `随日程进程组运行`。测试 `tests/test_process_autostart.py` 的 `test_rows_carry_autostart` 同步更新。
- **轮次日志不再夹带 dict 原文**（2026-11-14，体验 P2-3）：`[主循环轮次] 第 N 轮 {'live': 0, 'dynamic': 1, ...}` 直接来自 `f"...{res}"`，结构、引号、英文键名都被日志页当正文展示。现在由 `core/watcher.py` 的 `round_text()` 翻成 `直播 0 条 · 动态 1 条（关注流）· 已发 0 条 · 全开`，出错时是 `出错，跳过本轮：…`；tag 与 `[主循环轮次] 第 N 轮` 前缀都没变（日志页按 tag 过滤照旧）。测试 `RoundTextTest`。
- **QQ 的三个接口常量收敛到 `channels/qq_api.py`**（2026-11-14，审查细节 10）：`TOKEN_URL` / `API` / `INTENTS` 以前在 `official_qq.py`、`qq_inbox.py`、`qq_setup.py` 各写一份，连名字都不统一（`TOKEN_URL` vs `TOKEN_API`）。现在只在新模块里定义，三处 import，旧名字全部保留（`qq_setup.TOKEN_API` 是别名），外部导入不用改。

### 修复

- **日志页读不到日志**（2026-11-14）：三进程拆分后「日志」页一直是空的，`GET /api/logs` 返回 `files=[]`、`rows=[]`。两个原因叠加：① `scripts/run_console.py` 的 `--log-file` 默认指向 `out/console.log`，`ConsoleCore` 又从它的父目录推 `log_dir`，页面实际去 `out/` 找按日日志；② `core/logfmt.py` 的 `list_logs()` 只认 `bot-YYYY-MM-DD.log`，认不出拆分后的 `collector-*.log` / `pusher-*.log`。现在 `run_console.py` 新增 `--log-dir`（默认 `logs/`）并显式传给 `Console`，`ConsoleCore.__init__` 接受 `log_dir`（老入口 `run_bot.py --web` 不传时仍按 `--log-file` 的目录）；`logfmt` 现在认 `bot` / `collector` / `pusher` 三个家族（文件记录带 `kind` / `label`），「最新一天」把当天的采集 + 推送两份一起读、合并后按行首时间重排再切 `lines`，每行带 `src` 标明来源；页面日期下拉按天合并成一项「2026-11-14 · 采集+推送 · 23KB」。测试 `tests/test_logfmt.py` 加 7 例（家族识别、跨文件合并排序、src、旧 bot 日志兼容、非按日文件被忽略、log_dir 接线），`scripts/selftest_logs.py` 加 5 条断言；离线 634 例通过（1 例按环境跳过）。改完要重启监控台才生效。详见 [docs/12](docs/12-Web管理台.md) §8。
- **手填输入认靓号房间**（2026-11-14）：`/api/resolve` 原来把「房间短号」和「同号 uid」一律当歧义、按「谁在播算谁」处理，输入 `6` 时若 uid=6 的主播恰好在播就会跑到别人房间去。现在 `room/v1/Room/get_info` 返回的 `short_id` 恰好等于输入时一律按房间解释（`6` → `7734200`、`10` → `504273`、`25` → `21551834`、`616` → `8016907`），另一种解释继续放 `alt`；`live.bilibili.com/6` 这种短号链接同样适用。测试 `tests/test_livedeck_resolve.py` 加 1 例（现 16 例）。详见 [docs/23](docs/23-多屏直播监控台.md) §4.10。
- **40054007 长度超限不再首次即死信，死信补上 raw 原文**（2026-11-14）：`data/bot.db` 里 delivery 676（事件 `435243735:live_start:1793961024`，礼墨Sumi 开播）首次发送就被 QQ 判「消息发送失败, 长度超长」（40054007），`attempts=0` 直接进死信；而这条事件用当前模板重渲染只有 **77 字（127 字节）**——说明长度不只由 content 决定，富媒体消息的 `media.file_info`（约 1300 字符）与整个请求体也算在内。以前 `OfficialQQChannel._classify()` 把 400 一律判 DROP，`Dispatcher.deliver_one()` 的 DROP 分支与「重试耗尽」分支又都不写 raw，于是死信只剩一行 last_error、`raw=None`，当时到底发了多大完全无从复盘。现在官方通道**不猜平台上限**（文档只有一句「请缩短消息内容」），改成用回执驱动降级：收到 40054007 先把 media 去掉、以 `msg_type=0` 重发纯文字；仍超长再按比例压正文（`_clip_text` 保首行标题与末行链接，地板 `MIN_TEXT_CHARS=40` 字），最多 3 级（`MAX_DEGRADE_STEPS`）。成功即判 OK，raw 里记 `degraded`（如 `["no_media","clip=45"]`）、`original_request`（降级前的 content_chars / content_bytes / file_info_chars / body_bytes）与 `media_note`，`Dispatcher`「已发送但未带图」的告警因此能直接说出原因。其它 400（例如 40054013 用户拒收）不参与降级，仍是一次 DROP，不做无意义重发。**明确不分片**：单聊主动消息每用户每月只有 4 条配额，且 `(event_id, target_id)` 唯一，把一条事件拆成多条既吃配额又撞唯一约束，所以选择截断/降级重发。同时两条发送侧死信路径也补写 raw：`{"event":…, "target":…, "send":{"status","error_code","error_msg","channel_raw"}}`，`channel_raw` 只记长度不记正文（正文可由 event 重渲染），仍走 65536 字节上限——下一例长度类死信就能直接看出「原始多大、最后压到多小」。`scripts/run_bot.py` 退出时的死信摘要也跟着改：先把 raw 从摘要里摘掉打一行，再单列 300 字原文开头（完整原文看管理台「投递」页），否则 4000 字预览会把日志撑成一大坨。测试新增 `tests/test_qq_length_degrade.py`（6 例：富媒体被拒退纯文字、正文压短仍保链接、一直超长仍 DROP 但保留两份摘要、上传失败与压正文的两条 note 合并、非 40054007 不降级、401 刷 token 后重打）与 `tests/test_delivery_raw.py` 的 `DispatcherSendRawTest`（2 例：DROP 与重试耗尽的死信都带 event+target+发送证据），离线 642 例全部通过。详见 [docs/10](docs/10-通知层实现说明.md) §3.1/§3.3/§3.4、[docs/17](docs/17-卡片图与图片推送.md) §5。
- **同一条投递可能被发两遍：结果落库改成「领取令牌」fencing**（2026-11-14，审查 A1）：`claim_due()` 领取时把 `status` 置 `sending`、写 `claimed_at`，但 `_finish()` 的五条 `UPDATE` 只按 `delivery_id` 定位。`_reclaim_stale_claims()`（默认 300 秒，采集进程每 tick 也在跑）会把「领了没发完」的 `sending` 打回 `pending`，于是：A 领取 → 卡住超过 300 秒 → 被回收 → B 领取并发送成功 → A 的迟到结果落库，把 B 的 `sent` 改回 `pending` 或覆盖成失败，下轮再发一次。QQ 主动消息每用户每月只有 4 条配额，重复发的代价是实打实的。现在 `Delivery` 多一个 `claim_at`（本次领取时间）作为令牌，`SQLiteStore._finish()` / `MemoryStore.finish()` 都要求 `status='sending' AND claimed_at=<令牌>` 才落结果，返回 `False` 表示「这轮已经不归我管」；`Dispatcher._settle()` 统一记一行 warn 丢弃、不再重试；不传 `claim_at` 的老调用（自测脚本、给历史死信补写 `raw`）行为不变。测试 `tests/test_review_fixes_2026_11_14.py` 的 `SqliteFenceTest` / `MemoryFenceTest`（迟到的 OK/RETRY 都被丢弃、新令牌能落、无令牌的补写仍生效）。
- **投递页能「忽略」了：失败不再只有「重投」一条路**（2026-11-14，审查 A8）：以前死信只能重投，而重投是无条件 `UPDATE delivery SET status='pending', next_at=0, attempts=0, last_error=NULL`——永久错误（例如 QQ 返回 400 用户拒收）重投多少次都还是失败，只会反复占用发送配额；顶栏「失败 1」那个徽标是个纯 `<span>`，点不动。现在：① 新增 `POST /api/deliveries/{id}/dismiss`，把 `failed` 置 `dismissed`（「确认不发了」的终态，随事件过期一起清掉，`_prune()` 已把它算进可清理状态），误点还能再重投；② `retry` / `dismiss` 都只接受 `failed` / `dismissed`，其它状态 409（`只有失败或已忽略的投递可以重投（当前状态：%s）`），不存在的 id 404，不再「重投一条已经发出去的」；③ 「忽略」按钮只出现在失败行，重投前弹一次确认；④ 顶栏「已发 / 待发 / 失败」三个徽标可点，直接跳到对应状态筛选的投递列表（`gotoDeliveries()` 先设筛选再切页，避免先按旧筛选拉一遍）；⑤ 错误列限宽 260px、单行省略、悬浮看全文，不再把整张表撑开；⑥ 投递列表多带 `url`，动态类事件（`title` 为空）不再显示空白列，与事件页一样回退成链接。测试同上文件的 `ViewsClosureTest`（6 例：总数、标题回退、重投状态校验、404/409 文案、dismiss 后失败数归零且可再投、路由打通）。
- **事件页「共 80 条」其实是本页条数**（2026-11-14，审查 A7）：`/api/events` 只返回 `limit` 条（默认 50，页面要 80），页面拿 `o.events.length` 当总数显示，于是概览说 1219 条、事件页说「共 80 条」，看起来像事件被删了。现在 `/api/events` 返回 `{ok, events, total}`，`total` 走新增的 `ConsoleViews.events_total(uid=)`（带主播筛选时按同一 `uid` 统计），页面显示表内总数，超出本页时追加「· 本页显示最近 80 条」。测试：`tests/test_uid_range.py` 的 `test_events_query_uid`（总数必须按同一 uid 过滤）、`tests/test_review_fixes_2026_11_14.py` 的 `ViewsClosureTest`。
- **日志页 3 秒刷新会把正在回看的你拽回底部**（2026-11-14，审查 A6）：`loadLogs()` 每次渲染完都 `wrap.scrollTop = wrap.scrollHeight`。现在照监控室日志面板的老做法先算「是否贴底」（首次渲染或原本就在底部 30px 内才自动滚），翻上去看历史时不再被打断。
- **结束「监控室」进程前先核对它到底是谁**（2026-11-14，审查 A4）：进程页停/重启监控室时，PID 取自 `netstat` 的 LISTENING 行（或 `data/stack.json` 里记的值），随后直接 `taskkill /F`。8760 端口如果被别的程序（比如另一个 Edge 实例或别人的服务）占着，就会把不相干的进程杀掉。现在杀之前用 `Get-CimInstance Win32_Process` 取「进程名 + 命令行」，必须是 Python 且命令行含 `livedeck` / `run_livedeck` 才动手，否则记一行 warn 并返回 409：`端口 %d 上的进程（pid=%s）不是本项目的监控室，已拒绝结束；请先确认那个端口被谁占用`；同时 `taskkill` 的退出码不再被忽略（失败就是失败，不会假装「已停止」）。测试同上文件的 `TaskkillGuardTest`（9 例）。
- **过期事件不再连带删掉还没发完的投递**（2026-11-14，审查 B-5）：`_prune()` 先按 `status IN (sent, failed, dismissed)` 删终结投递，再 `DELETE FROM event WHERE occurred_at<?`；v4 起 `delivery.event_id` 带 `ON DELETE CASCADE`，于是 docstring 承诺「pending/sending 的投递永远不动」并不成立——只是默认 `event_days=180`，要一条投递挂半年才会撞上。现在删除事件时排除「有 pending/sending 投递」的事件（`keep_event_sql`），`MemoryStore.prune()` 同步同样的规则。测试 `tests/test_review_fixes_2026_11_14_b2.py` 的 `PruneTest`（SQLite 与内存两份，断言只剩被卡住的那条事件、投递还在）。
- **未来时间戳不会再把动态水位永久抬高**（2026-11-14，审查 B-6）：`_digest_items()` 的 `last_poll_ts=max([st.last_poll_ts] + [i.pub_ts for i in items])` 把这一页的最大 `pub_ts` 当水位，只要 B 站返回一条未来时间戳（预约动态、时钟异常），水位就停在未来，之后所有正常动态都会被 `it.pub_ts <= st.last_poll_ts` 当成置顶旧条目跳过——表现是静默不推、日志里毫无异常。现在新增 `_clamp_watermark()`：水位最多记到「现在 + 300 秒」，超出的写一行「[动态] … 返回未来时间戳 N（比现在晚 X 分钟），水位只记到 M」。测试 `WatermarkTest`。
- **「已见」标记推迟到入库之后，入库前崩溃不再永久漏推**（2026-11-14，审查 B-7）：`_digest_items()` 在产出事件后立刻 `mark_seen`，真正入库（`insert_event` + `enqueue`）在后面的 `publish()` 里——两步之间被杀（升级、断电、崩溃）这条动态就再也不会被推。现在改为：置顶旧条目 / 迟到 / muted / 未知类型仍当场记（它们本来就不产事件），产事件的条目等 `publish()` 入库成功后由 `_mark_published()` 补记；`sweep_dynamic()` 被 `sweep_late_max` 裁掉的那部分也先补记，避免下一轮重复处理。代价是入库失败时会重试一轮（宁重不漏）。测试 `MarkAfterPublishTest`（顺序断言严格为 insert → mark），`tests/test_enrich.py` 的 `test_digest_survives_unfixable` 同步更新契约。
- **退避系数收成一张表，HTTP 412/429 不再被当业务风控吞掉**（2026-11-14，审查 B-12/B-13）：三条路径各写一套退避（`collector/bili.py` 2/3/0.3、`collector/follow_feed.py` 1.5/2/0.5、取流 2/3/0.4），同一种「网络抖一下」等的时间都不一样；更麻烦的是 `BiliError` 只有 `code`，网络错记 `-1`、HTTP 非 200 记 `-status`、业务码原样记，而 `RISK_CODES = (-352,-412,-509,-799)` 与 HTTP 412/429 数值撞车——`fetch_dynamics()` 拿到 HTTP 429 的 `-429` 判成非风控直接抛，HTTP 412 反而被当业务风控跳过。现在：`BiliError` 多一个 `kind`（net / http / biz，默认 biz）与 `is_risk`（只有 `kind == biz` 且命中 `RISK_CODES` 才算撞风控），退避走 `backoff_seconds(kind, attempt, code)`（net 2×、http 3×、biz 除 `-352` 仍 0.3 秒外 3×）；`RISK_CODES` 提到模块级、`follow_feed.py` 改为 import，不再各写一份。测试 `BackoffTest`。
- **SESSDATA 过期会自动退回逐人轮询**（2026-11-14，审查 B-14）：`_check_login_state()` 的 docstring 写着「自动退回匿名」，实现只有一行告警；关注流遇到 `-101` 时 `-101` 不在 `RETRYABLE` 里，`round_dynamic_feed()` 只 `feed_errors += 1` 然后返回空列表——动态静默停更，页面上「在监控」一切正常。现在 `-101` 首次出现就置 `_auth_bad` 并打一行「[关注流] B 站登录态失效 → 回退逐人轮询（动态照常收，只是慢一些；续期成功会自动切回关注流）」，`feed_enabled()` 见到 `_auth_bad` 直接 False，采集按人照常抓；登录恢复后自动清掉标记切回关注流。测试同上文件的 `test_feed_auth_failure_falls_back`，文档见 [docs/35](docs/35-关注流采集.md) §11。
- **关注流写 Cookie 与计数补锁**（2026-11-14，审查 B-15）：`follow_feed.py` 覆写 `_apply_cookies()` 时没进 `self._lock`，`request_count += 1` / `last_risk += 1` 也是裸自增（父类走的是加锁的 `_bump_request()`），多线程下计数会丢更新。现在方法体整体持锁（`RLock`，父类 `_ensure_buvid3()` 持锁调用可重入），计数统一走 `_bump_request()`、风控计数加锁。
- **日志「全部日期」加了扫描预算**（2026-11-14，审查 B-9）：`read_logs()` 在 `date=all` + 强过滤（例如只看 `error`）时会逐份把每个日文件读 2MB，日志攒到几百天以后一次查询就是几百 MB。现在最多扫 **90 份日文件 / 累计 32MB**（`MAX_SCAN_FILES` / `MAX_SCAN_BYTES`），撞到上限返回 `limited=true` 与 `scanned_bytes`，日志页在统计行后面追加「已到扫描上限（只扫最近 N 份 · X MB，想更早请选具体日期）」。测试 `LogScanBudgetTest`。详见 [docs/12](docs/12-Web管理台.md) §8。
- **FLV 上游连接不再无限等**（2026-11-14，审查 B-8）：`livedeck/app.py` 的 `stream_http` 用 `read=None`（永不超时），上游 CDN 静默挂住、或浏览器那头不读时，`iter_bytes` 会一直卡着，线程与连接都不回收。现在读超时 30 秒（FLV 正常每帧都有数据，30 秒没有字节必是坏连接）。
- **`no_proxy` 的手填值会把本机豁免冲掉**（2026-11-14，审查 B-1 残留）：`shared/proxy.py` 的 `validate()` 以前是 `str(...).strip() or DEFAULT_NO_PROXY`——只要填了值就整串替换，手工写一个 `example.com` 就把 `127.0.0.1,localhost,::1` 弄丢，监控台自己调 8760 / CDP 的请求会被送进代理（表现为监控室空白、浏览器连不上）。现在 `merge_no_proxy()` 永远把三个本机地址放在最前、再把用户条目追加去重，`;` 也当分隔符。报告另半条「代理地址明文回显」**不改**：设置弹窗是「读出来填进输入框、改完整体提交」的表单，把 `user:pass` 遮成 `****` 之后用户一点保存就会把 `****` 当新地址写回配置，代理直接坏掉；该接口本来就在环回 + CSRF 之后，看到的就是自己刚填的那串（`status()` 的 docstring 写了这个理由）。测试 `NoProxyMergeTest`（5 例）。
- **监控室「在播」与 LIVE / OFF 三个数据源打架**（2026-11-14，体验 P1-13）：顶栏只数 `st.snap.bridge.rooms` 里的 `live_status === 1`，格子角标优先用 `st.info` 的房间信息，左栏名单又走另一条节流路径，于是会出现「顶栏在播 0 / 名单 LIVE / 画面在播」。现在统一成 `liveStatusOf(id)`（先看 `st.info` 的 `known` 值，再退回 `st.rooms` 同房间的值，都没有算未知），顶栏计数、名单徽标、格子角标全部调它；未知态显示「待连接」并给悬浮说明，不再与「未开播」共用 OFF。测试 `FrontEndTest`。
- **事件 / 投递的类型字段补转义**（2026-11-14，审查细节 2）：事件页与投递页的 `e.type` / `e.type_label` 直接拼进 HTML 属性，同函数里的标题、URL 都过了 `esc()`。取值链目前全是代码常量（不可利用），但这属于「同一条模板一半转义一半不转义」的坑，一并补上。
- **投递目标表的外链 id 能看全文了**（2026-11-14，体验 P1-10）：`external_id` 为了不撑破表格只显示前 18 字 + 省略号，需要自己对账时没法复制。现在单元格带 `title` 悬浮显示全文（仍不明文铺满整列，也不打码——本机管理员本来就需要核对 openid）。
- **页面 favicon 不再 404**（2026-11-14，体验 P2-1）：`web/index.html` 没有 `<link rel="icon">`，浏览器每开一次页面就请求一次 `/favicon.ico` 并 404。现在用内联 `data:image/svg+xml` 给一个深底蓝框粉三角的小图标，不新增静态文件、不新增请求。

### 安全

- **卡片图下载没有边界：补上白名单 / 20MiB 上限 / content-type 校验**（2026-11-14，审查 A5）：`notifications/cards.py` 的 `_img()` 自己写了一套下载逻辑——`follow_redirects=True`、只判 `startswith("http")`、`Image.open(r.content)` 整包读进内存，既没有域白名单也没有字节上限，与 README 里「图片下载只允许 B 站图床、流式硬限 20 MiB」的承诺不一致（`core/weekly/fetch.py` 的周表图早就有这层边界，卡片图这条路没跟上）。现在复用同一份实现：先过 `_img_url_ok()`（只允许 `hdslb.com` / `bilibili.com` / `hdslb.cn`，拒 userinfo、拒非 http(s)），跳转后对最终地址再复检一次，`content-type` 必须是 `image/*`，按 64KB 流式累加、超过 `DOWNLOAD_MAX_BYTES`（20MiB）立即中断，失败不写缓存也不重试。本地 `file:` 路径只允许读 `data/cache/img/` 之内（`scripts/preview_cards.py` 的离线预览照常可用）。测试同上文件的 `CardImageBoundsTest`（12 例：非白名单、环回地址、userinfo、ftp、302 到内网、text-html、超限、空响应、缓存命中只请求一次、目录内外本地图）。
- **敏感信息脱敏：死信 raw 不写 openid，异常栈统一过脱敏器**（2026-11-14，审查 A2/A3）：① 发送侧死信会把 `target` 序列化进 `delivery.raw`，`external_id`（QQ 的 `user_openid`）因此以明文落库，管理台再原样回显——直接违反 AGENTS §4「数据库里不写 user_openid 明文」。现在 `_dump_raw()` 落库前递归脱敏 `external_id` / `openid` / `user_openid` / `union_openid` / `open_id`（保留末 4 位便于对账），页面上看到的是 `****abcd`。② `traceback.print_exc()` 直接把原始栈打到 stderr，栈里可能带请求 URL 与本地路径；`web/console_http.py` 与 `livedeck/server.py` 三处改成走日志 + `scrub_text(traceback.format_exc())`。顺带把 `collector/bili.py` 里那套脱敏函数整体搬到 `shared/redact.py`（`scrub_text` / `scrubbed` / `mask_sessdata` / `mask_secret` 等）——它们此前在库内**零调用点**，等于死代码，README 里「日志与异常统一脱敏」的承诺并没有真正兑现；`collector/bili.py` 保留同名再导出，调用方不用改。测试：`tests/test_delivery_raw.py` 两处断言改为「原文不外泄」（`self.assertNotIn("openid-1", item["raw"])`）。
- **监控室不再接受非环回绑定**（2026-11-14，审查 B-2）：`livedeck/server.py` 的 `Handler.require_loopback` 是按「绑定的 host 是不是环回」算的，一旦 `--host 0.0.0.0`，对端环回与 `Host` 头两项检查会一起失效；剩下的 `Origin` 比对两个值都取自请求头，DNS rebinding 下等于自己证明自己。现在 `serve()` 启动时先判 `webutil.is_loopback_host(host)`，不是环回直接 `SystemExit`（提示走 SSH 隧道转发 `127.0.0.1:8760`），`--host` 仍可传 `localhost` / `::1`，只是不再接受对外地址。测试 `tests/test_review_fixes_2026_11_14_b2.py` 的 `LoopbackOnlyTest`。详见 [docs/23](docs/23-多屏直播监控台.md) §5。
- **FLV 直链补白名单**（2026-11-14，审查 B-3）：`livedeck/deck_bridge.py` 的 `_playinfo()` 把 B 站 `playUrl` 响应里的 `host + base + extra` 直接拼成上游地址交给 `stream_ws` / `/api/flv` 转发，全程没有校验——上游响应一旦被投毒，这里就是一条通往内网的通道。新增 `shared/netsafe.py`（`host_matches` / `url_allowed` / `stream_url_ok` / `qq_gateway_ok`），只放行 `.bilivideo.com` / `.bilivideo.cn` / `.hdslb.com` / `.hdslb.cn` / `.bilibili.com` / `.szbdyd.com` 的 http(s) 地址并拒 userinfo，不在名单里 502「B 站返回的流地址不在白名单内，已拒绝转发」。报告里「也会跟随 302 到内网」不成立（该 client 没开 `follow_redirects`），所以没有为此加禁跳转逻辑。
- **QQ gateway 地址补白名单**（2026-11-14，审查 B-16）：`channels/qq_inbox.py` 与 `channels/qq_setup.py` 拿到 `GET /gateway` 返回的 `url` 就直接 `websockets.connect()`，IDENTIFY 帧里还带着 `QQBot <access_token>`。现在两处都先过 `qq_gateway_ok()`（只 `wss://`、主机必须是 `qq.com` 或其子域），否则拒绝连接并提示检查 DNS/代理。

## [0.3.6] - 2026-11-14 — 代理三模式与手填自动识别

这一版两件新增，都是冲着手感去的：网络代理从「开 / 关」改成「不使用 / 系统 / 自定义」三选一，监控台（8765）与监控室（8760）共用一份 `config/proxy.json`，保存后当前进程立即生效；左栏底部输入框不再要求先选类型，房间号、主播 UID、直播间链接直接填，只有真认不出或真歧义时才提示。离线用例 626 例（1 例按环境跳过）。

### 新增

- **网络代理设置**（2026-11-12）：监控台（8765）与监控室（8760）的「设置」菜单各多一项「网络代理」，打开同一个配置弹窗，三选一——**不使用代理**（主动删掉 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`（含小写），系统里设了也不走）/ **使用系统代理**（默认，完全不动这些变量，沿用系统 / 启动脚本里的代理）/ **自定义代理**（把代理变量写成下面填的地址）+「测试连接」（服务端按当前模式真的访问一次更新检查地址，`none` 直接回「不用测试」，`system` 没配环境变量会明说）+ 保存 / 取消，右侧小字跟着模式显示「跟随系统 / 不使用 / 自定义」。只在自定义时代码框可编辑；切换模式不会清掉已填的地址。配置落在 `config/proxy.json`（`mode` / `url` / `no_proxy`，真实文件在 `.gitignore` 里，模板 `config/proxy.json.example`，`scripts/preflight.py` 会校验；旧版只有 `enabled` / `url` 的配置仍能读——开着且有地址按自定义，其余按系统代理），两个页面改的是同一个文件。生效方式是把 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`（含小写）与 `NO_PROXY` 写进进程环境变量（`shared/proxy.py` 的 `apply_to_env()`，自定义写入、不使用删掉、系统代理还原成启动时看到的值）：项目里六处 `httpx.Client` 与 urllib / websocket-client 出口都是默认 `trust_env`，所以一次注入全覆盖；四个入口脚本（`run_console.py` / `run_bot.py` / `run_pusher.py` / `run_livedeck.py`）启动时各调一次，保存后当前进程立即生效，已在跑的后台进程要重启才整体生效。只支持 `http://` / `https://`——httpx 走 socks5 要额外的 socksio 依赖，按 §5「不新增运行时依赖」明确拒绝并给出提示；`no_proxy` 默认 `127.0.0.1,localhost,::1`，本机 8765 / 8760 / CDP 端口不会被绕去代理；浏览器里的画面弹幕（本机 Edge 的 CDP 桥）与 QQ 收消息的 `websockets`（库不支持 HTTP 代理）不走代理，弹窗里都写明了。接口 `GET /api/proxy`（返回 `mode` / `mode_name` / `enabled`（兼容旧前端）/ `active`）、`POST /api/proxy`、`POST /api/proxy/test`（永远不抛异常，返回 `{ok,passed,status,ms,target,error}`）。测试 `tests/test_proxy_settings.py`（29 例：三模式常量 / normalize / validate / 旧 enabled 兼容 / save-load / apply 三种模式与还原 / probe 假 client 与系统代理缺变量 / 两侧路由与前端契约 / 入口接线 / preflight 与示例文件 / 文档），离线用例目录现为 626 例（1 例按环境跳过；比上一版 v0.3.5 的 580 例多 46 例，代理与手填识别两批各占一部分）。详见 [docs/12](docs/12-Web管理台.md) §22.13、[docs/23](docs/23-多屏直播监控台.md) §2。



## [0.3.5] - 2026-11-11 — 加房不再卡在「引擎启动中」

这一版只修一处，但很影响手感：空舞台加房后，如果那一轮房间 token 全取不到，浏览器就一直没被拉起，页面永远停在「引擎启动中」；现在这种分支也会先拉起浏览器，并且连续 30 秒「启动中」会自己再 kick 一次。离线用例 580 例全部通过。

### 修复

- **空舞台加房后一直停在「引擎启动中」**（2026-11-11）：上一版加的「空舞台加房即刻连引擎」有个漏网场景——`_sync_bridge()` 遇到房间 token 全部取不到（且内存里没有未过期的旧 token）会直接早返回，从不调用 `ensure()`，浏览器于是从未启动，桥停在 `idle`/`stopped`，对外就一直是 `starting`（页面「引擎启动中…」），只能等到下一次体检或 token 恢复。现在这条分支先把浏览器拉起来：起不来写 `error`「引擎启动失败：…」，起来了再等下一轮取 token。同时 `mark_starting()` 改成 0.5 秒超时取锁，不会被 `start()` 那把手锁挡住；新增 `_starting_guard()`（`STARTING_GRACE = 30` 秒，跟 15 秒体检跑）——连续 30 秒都是 `starting` 就记 warn 并再 kick 一次「启动中超时自愈」，不再被动等异常检测。`_engine_kick()` 补了「引擎即刻连接：<为什么>（原状态 X）」与「引擎即刻连接完成」两条 info，事后能查。测试：`tests/test_livedeck_engine.py` 的 `StartingGuardTest`、`EmptyItemsSyncTest`，`tests/test_livedeck_bridge.py` 的 `test_mark_starting_skips_when_lock_is_busy`。详见 [docs/23](docs/23-多屏直播监控台.md) §5.5。

## [0.3.4] - 2026-11-11 — 顺手一点：开机自启、头像提速与引擎状态分明

这一版集中在「日常打开监控台的那几秒」：进程页能设开机自启，概览能直达绑定，监控室的头像与弹幕引擎状态不再等轮询；同时修掉两个真问题——0.3.3 启动链路上任务开关必然 500，发布包漏了 `shared/tmpdir.py` 与三张守卫配图。离线用例 574 例全部通过。

### 新增

- **进程页每行多了「开机自启」开关**（2026-11-11）：采集 / 推送 / 弹幕监控台 / 录播清理守护四行各一个勾选框，语义是「随监控台启动」——勾上则下次 `run_console.py` 起来时顺手拉起，不是另装 Windows 计划任务（监控台自身的开机自启仍归 `scripts/service.ps1`，守护与主进程两行只读显示「计划任务 / 启动文件夹 / 未设」）。落库在 `meta['process_autostart']`，只保存与默认值不同的 key（采集、推送默认开，保持 0.3.x 的现状；弹幕监控台、清理守护默认关，升级后不会凭空多进程）；改开关只落库、不启停当前进程，下次启动生效。应用点在 `run_console.py` 的 `stack.start()` 之前（`console.apply_autostart()`），采集/推送经 `StackManager.set_autostart()` 参与 `desired()` 判定（手动开 > 手动关 > 自启关 > 绑定命中）。接口 `POST /api/tasks/autostart {key,on}`，见 [docs/12](docs/12-Web管理台.md) §22.11；测试 `tests/test_process_autostart.py`（22 例）。
- **房间头像改成「缓存 + 并发补齐 + 即时推送」**（2026-11-11）：左栏三十多行的头像原来靠串行请求（每次间隔 0.12 秒）慢慢铺，启动要好几秒才齐。现在头像与昵称单独缓存在 `data/livedeck_faces.json`（`uid → {face, uname, ts}`，不含登录信息）：启动时先把缓存播种进内存，第一帧就有头像；超过 6 小时的在后台刷新、7 天算硬过期、落盘时清掉 60 天没更新的条目。`_prime_faces()` 改成 6 路线程池并发拉（34 个头像实测 1.25 秒，原来 5 秒以上），**拉到一张就推一张**——新增 SSE 事件 `{"type":"room_face","room","uid","face","uname","ts"}`，页面只更新那一行并节流重绘左栏，不用再等 12 秒一次的 `pollSnap()`。拉取失败不写缓存，下一轮重试。头像为空或破图时渲染首字母占位（`.av.ph`），不再出现 `<img src="">` 的空槽。测试见 `tests/test_livedeck_faces.py`（14 例）。详见 [docs/23](docs/23-多屏直播监控台.md) §4.9。

### 变更

- **取数方式下拉可选「自动」，且不再被定时刷新覆盖**（2026-11-11）：概览「动态采集方式」的下拉原来只有「关注流 / 逐人轮询」两项，而配置默认值是 `auto`——页面把它硬显示成「关注流」，用户选了别的又被每 3 秒一次的轮询打回去，看起来像「自动跳回关注流」。现在第一项是 `auto`「自动（有登录凭据时走关注流）」，与 `poll.mode` 一一对应；改动下拉只点亮「切换（需重启）」并提示未保存（`feedModeDirty`），**不发请求、不自动保存**，因此不影响实际采集方式，也不会被定时刷新覆盖；提示里同时写清「配置为 X、实际跑 Y」（`mode_effective`）。详见 [docs/12](docs/12-Web管理台.md) §22.12。
- **概览推送目标为空时给按钮直达机器人页**（2026-11-11）：以前只有一行「还没有推送目标（用 --channel qq 启动主循环会自动写入）」，指向一个已经不适用的旧跑法。现在文案改成「先到「机器人」页把 QQ 机器人的 openid 绑上，绑好并重启采集进程后这里会自动出现」，并给「去机器人页绑定 openid」按钮，切页后平滑滚到 QQ 绑定卡片（`gotoQQBind()` → `#qqBindCard`）。
- **采集进程没在跑时，名单房间的开播状态改由监控室自己轮询**（2026-11-11）：`subject_state.live_status` 只有采集进程活着才会更新，采集没绑定 / 被停掉 / 崩了的时候名单会一直停在旧状态（下播了还亮 LIVE）。新增 `config.collector_alive()` 读监控台写的 `data/stack.json`（`StackManager` 每 2 秒刷新，`updated_at` 超过 60 秒视为判断不了），`DeckInfo._own_status()` 据此缓存 20 秒；采集没在跑或读不到状态时，名单房间进 `_info_watch_list()` 的 `db` 档（在播 60 秒 / 未开播 180 秒自己刷），采集正常时仍然只读、不重复请求。顺带纠正一个误判：`subject_state.last_poll_ts` 是动态流水位（主播不发动态就不动），不能当心跳用。详见 [docs/23](docs/23-多屏直播监控台.md) §4.5。
- **弹幕引擎状态分家：等待选台 ≠ 引擎故障**（2026-11-11）：`/api/snapshot` 的 `bridge.status` 现在是对外口径——舞台与只听都为空时报 `waiting`（顶栏灰点「等待选台」），有房间但桥还没起来报 `starting`（琥珀点「引擎启动中…」），只有真的出错才是 `error`/`stale`（红点「引擎离线」）；桥的原始状态保留在新字段 `bridge.state`，排查照旧看它。`EngineWatch` 遇到 `waiting` 不再按「离线 90 秒」报 error，改为在进入该状态时记一条 info「舞台为空，弹幕引擎待命：等待选台」。
- **引擎状态变化即时推送**（2026-11-11）：新增 SSE 事件 `{"type":"engine","status","state","error","ts"}`，桥状态一变就推（按状态+错误去重，15 秒一次的健康体检也会补推），页面顶栏不用再等 12 秒一次的 `pollSnap()`。
- **空舞台加房即刻连引擎**（2026-11-11）：`set_active`/`set_listening` 发现「原先舞台为空、这次有房间」就调 `_engine_kick()`——先把状态置 `starting` 并推 SSE，再在后台线程 `ensure()` 拉起浏览器，成功才同步房间。同时修掉一个会让它永远起不来的老问题：`stop()` 留下的 `_stopping=True` 以前只有 `ensure()` 的 online/stale 分支会复位，空舞台清过房间之后再加房，`start()` 会直接返回 False。
- **每个房间的弹幕 token 并行获取**（2026-11-11）：`_bridge_items()` 里取 token 是一次 WBI 签名 + HTTP 往返，16 路串行要好几秒，现在用线程池（最多 4 并发）同时取，仍按舞台顺序拼装；单房失败回退未过期的旧 token，没有旧 token 就跳过该房并写 warn。

### 修复

- **发布包里少了 `shared/tmpdir.py` 和弹幕监控台的三张配图**（2026-11-11）：`scripts/release.py` 的两条宽规则误伤了自己人——`tmp*` 把 `shared/tmpdir.py` 当成 tempfile 残留排除（包里一 import 就 ImportError，`run_livedeck.py` 起不来），目录名 `img` 则把 `livedeck/static/img/` 的 guard1/2/3.png 一起排掉（页面能开，守护位空白）。现在临时产物只认 `tmp` + 8 位随机后缀（`is_tmp_artifact()`，文件和目录都判），文档配图改成按路径精确排除 `docs/img`；四个文件加进 `REQUIRED_FILES`，新增 `tests/test_package_assets.py` 依次断言「文件存在 → `should_pack()` 放行 → 真写进 zip 名单」。
- **进程页的任务开关在 0.3.3 的启动链路上必然 500**（2026-11-11）：开关落库靠进程级单例 `T.BOARD`，旧链路由 `run_bot.py` 绑定；换成 `run_console.py` 起监控台之后**没有任何地方 bind**，点一次「暂停」就抛 `RuntimeError("没有绑定数据库，无法保存开关")` 包成 500。现在由 `ConsoleCore.__init__` 统一 `T.BOARD.bind(self.db_path)`（谁造管理台谁绑库），`scripts/run_console.py` 再显式绑一次做双保险；新增 `tests/test_console_board.py`，故意不预绑、直接 `set_task()` 验证能写进自己的库，并断言两个启动入口都保留这行。

## [0.3.3] - 2026-11-11 — 装上就能用：三进程拆分与页面内接入

这一版回答一个交接时最容易卡住的问题：**装完不填任何东西，也能先把监控台打开**。
账号与机器人都移到页面里绑，绑上后对应的后台进程自己起来；监控台主进程从此只服务页面，
B 站变慢或 QQ 抽风都压不到它的响应上。离线用例 509 例全部通过。

### 新增

- **三进程架构**（2026-11-11）：监控台主进程（`scripts/run_console.py`）只做三件事——服务页面、写心跳、托管两个后台进程；采集进程（`scripts/run_bot.py --collector`）只把直播与动态入库，`Watcher(..., dispatcher=None)` 因此**不派发、不清理缓存**；推送进程（`scripts/run_pusher.py`，新增）从持久队列里取投递、跑派发循环、收 QQ 指令、发清理通知。一条采集变慢不再拖住推送和页面，队列在库里，进程重启不丢。
- **按绑定状态自动开关**（2026-11-11）：新增 `shared/readiness.py` 作为「绑没绑好」的唯一口径（`bili_bound` 看 `config/bili.env` 的 SESSDATA，`qq_bound` 看 `BBOT.env` 的 AppID/AppSecret + `.p0_ids.json` 的 user_openid，占位值一律算没绑）。新增 `web/stack.py` 的 `StackManager`：每 2 秒对账一次，该起就起、该停就停，崩了按 5→300 秒退避重启，状态原子写 `data/stack.json`，日志 `logs/collector.log`、`logs/pusher.log`（按天落盘）。采集与推送**默认都关**，用户绑上 B 站账号 / QQ 机器人后自动开启。
- **管理台进程接口**（2026-11-11）：新增 `web/console_stack.py` 与 `GET /api/stack`（绑定状态 + 两个角色是否在跑、pid、重启次数、上次退出码、上次错误）、`POST /api/stack/action`（`role` = collector/pusher，`action` = start/stop/restart/auto；未绑定就启动会被拒并给出去哪绑的提示）。进程页多出「采集进程」「推送进程」两行，可单独启动 / 重启 / 停止，并能看出循环任务归属哪个进程（`core/tasks.py` 新增 `OWNER` 归属表与 `owner_of()`）。
- **首次打开就有接入指引**（2026-11-11）：顶栏常驻「接入指引」按钮（绑好后圆点变绿），首次打开且尚未绑全时自动弹出。四步按当前状态打勾：绑定 B 站账号（一键去扫码）、配置 QQ 机器人（去填 AppID/AppSecret、抓推送目标）、导入监控名单、确认采集/推送进程（可单独重启）。关掉后不再自动打扰，按钮随时可重看（`localStorage` 键 `bbot_guide_seen`）。

### 变更

- **安装流程不再要任何绑定信息**（2026-11-11）：`install.ps1` 由 4 步扩为 5 步——第 5 步在端口空闲时用 `scripts/service.ps1 start` 起服务（服务起不来则直接用 `pythonw run_console.py` 兜底），等到端口可访问后自动打开监控台，并尽力拉起监控室（`run_livedeck.py --port 8760`，起不来只警告）。新增 `-SkipStart` / `-NoOpen` / `-Port`；安装结束时打印监控台与监控室地址，以及「接下来全在页面里做」。
- **install.cmd 装完不关窗**（2026-11-11）：无论成败都停留并提示按任意键关闭，成功时打印监控台地址与「接入指引」入口；换行统一为 CRLF。
- **服务链路改为托管监控台**（2026-11-11）：`scripts/service.ps1`、`scripts/service_start.cmd`、`supervisor.py` 的子进程从 `run_bot.py --channel qq --web` 改为 `run_console.py --port 8765`（`service.ps1` 新增 `-ConsoleArgs`，`-BotArgs` 降级为兼容参数）；停止文件改为 `data/console.stop`。`supervisor.py` 的心跳优先读 `data/console.beat`，避免采集默认关闭时把正常页面误判为卡死。`service.ps1 status` 增加「采集 / 推送进程」与绑定状态两段。
- **任务卡文案按新分工改写**（2026-11-11）：进程页的说明改成「监控台主进程只服务页面；采集在采集进程里跑，派发 / 缓存 / 收件 / 清理通知在推送进程里跑」。

### 修复

- **install.cmd 的 LF 换行**（2026-11-11）：Windows 的 cmd 对 LF 结尾的批处理解析不稳（`goto`/`pause` 可能被跳过），已统一为 CRLF。
- **推送进程不再拿占位凭据去连 QQ**（2026-11-11）：`run_pusher.py` 原先只判空，`.p0_ids.json.example` 里的 `<openid>` 会被当成真目标，进而在收件循环里以 5/9/16/29 秒无限重连。现在用 `readiness.is_placeholder()` 与最小长度挡在开库、建通道之前，并给出「去哪绑」的一句话。

### 升级提示

- 从 0.3.x 升级：`scripts/service.ps1 stop` 之后 `scripts/service.ps1 start`，让自启链路换成新的 `run_console.py`；旧的 `run_bot.py --web` 仍可手动运行，但那个管理台没有进程托管能力（`/api/stack` 会返回 `available: false` 并提示）。
- 升级后首次打开监控台会弹出接入指引；已绑定的机器会直接显示「已绑定」，采集与推送随之自动启动。

## [0.3.2] - 2026-11-11 — 弹幕引擎可观测性与补漏推送

本版给监控室的弹幕引擎补上「过程」——日志与健康体检，让「离线」「连上了但没弹幕」事后能查；同时修掉两条会让用户少收东西的路径：关注流补漏只标记不推送、被名单对账停用的号加不回来。离线用例 442 例全部通过。

### 新增

- **弹幕引擎运行日志与健康监测**（2026-11-11）：监控室「引擎离线 / 连上了但一条弹幕都没有」以后能事后查。新增 `livedeck/engine_log.py`：`EngineLog` 把引擎关键节点写进 `logs/livedeck_engine.log`（UTF-8、LF、2 MB 轮转保留 3 份，`warn`/`error` 同时回显常规日志、`info` 只进文件），并在内存留最近 800 条；`EngineWatch` 每 15 秒体检一次，按五条规则写带排查提示的日志——引擎离线满 90 秒（error，附 `last_error` 与排查顺序）、认证通过但 90 秒零帧（warn）、180 秒没有新帧（warn）、连上 600 秒收到帧但 0 条 `DANMU_MSG`（warn）、`decode.dropped` 在涨（error，提示核对 `brotli`/`protover`），同一问题按 key 去重不刷屏。接入点：`bridge.py` 记录浏览器启动（pid/端口/profile）、DevTools 就绪耗时、CDP 命令超时、页面注入结果、**读线程退出原因**（以前被 `except` 吞掉）、桥重建；`hub.py` 每房新增 `frames`/`bytes`/`last_frame`/`danmu` 帧级指标并记录解码丢帧；`deck_bridge.py` 记录 token 请求失败/被拒、同步失败、`-352` 类重签。新增 `GET /api/engine?tail=`（默认 120、最多 600 条）与 `/api/diag` 的 `engine` 段（计数、最近 10 条、`last_error`、`frames` 总帧数）。新增 `tests/test_livedeck_engine.py`（14 例）。详见 [docs/23](docs/23-多屏直播监控台.md) §5.4。

### 变更

- **关注流补漏发现的遗漏动态改为补推送**（2026-11-11）：以前 sweep_dynamic 补到的条目只要比水位旧就**只记已见不推**，而关注流一轮会把水位顶到同批最新的一条，被吞掉的条目恰恰都低于水位 —— 补漏实际只能「标记已读」，用户收不到。现在 poll.sweep_push_late（默认 true）让补漏忽略水位（_digest_items(..., ignore_watermark=True)），补到的遗漏动态按发布时间正序照常推送；两条防刷屏：回看窗 sweep_max_age_min 由 30 分钟放宽到 **720 分钟（12 小时）**，更早的仍只记已见；单次补推上限 sweep_late_max（默认 30）条，超出部分只记已见并在日志写明「超过单次上限」。设 sweep_push_late: false 可退回旧口径。配置同步进 config/subjects.json.example、web/console_feed.py 与 scripts/run_bot.py，管理台补漏行文案改为「累计补到并推送」。tests/test_feed_watcher.py 的补漏用例由 1 例扩到 5 例（低于水位、回看窗内、超窗不推、开关关闭、上限保最新）。详见 [docs/35](docs/35-关注流采集.md) §5、§7。
- **README 增加「效果图」小节**（2026-11-11）：首页放三张本地实跑截图（`docs/img/readme-console-overview.png`、`readme-console-subjects.png`、`readme-livedeck.png`，依次是监控台概览、监控名单、多屏直播台），并注明截图已隐去账号、登录态与推送目标标识。
- **概览去掉「关注流会吞条目」的提示行**（2026-11-11）：监控台概览的取数说明里不再显示「关注流会吞条目（101 个关注实测漏 16.4%），所以每 N 小时逐人补漏一次；补到的旧动态只记不推」这句，`/api/feed/status` 的 `notes` 里同步删掉同一条（前端本来就没用它）。补漏机制本身不变，代码注释与 docs/35 里的实验数据保留。

### 修复

- 修复已被名单对账停用的账号无法重新加入监控名单的问题。

## [0.3.1] - 2026-11-11 — 弹幕桥离线修复与发布收尾

第三位递增（patch）：修好监控室「弹幕引擎离线」（Edge profile 目录 ACL 不全导致浏览器静默退出），
「更新检查」改跟发布线 `opensource` 比对（不再 404），并把这轮发布相关的脚本、文档和测试一起收尾。

### 新增

- **「更新检查」弹窗与「关于」里的源码 / 反馈链接**（2026-11-10）：两侧设置菜单里的「更新检查」从灰显改成真正可用——点开弹窗（监控室 `#upd-mask` / 监控台 `#updModal`），服务端去 Gitee 读 `VERSION` 与本机比一下，**只读、不下载、不升级**。新增 `shared/update.py`（`parse_version` / `is_newer` / `fetch_latest` / `check`，按 `opensource` → `master` → `main` 的顺序试 `raw/<分支>/VERSION`（`opensource` 是发布线，排第一是为了跟已发布版本比））与新路由 `GET /api/update`（`livedeck/server.py` 与 `web/console_views.py` + `console_http.py` 分派，和 `/api/version` 同一条链）。弹窗三种状态（发现新版本 / 已是最新 / 检查失败），关闭走右上角 ×、「关闭」、点遮罩、Esc 四处。同一弹窗的「关于」里，「源码」「反馈」从「暂无，后续补充」改成 `https://gitee.com/victorkiseki/bili-monitor` 与同仓库 `/issues` 的链接（新标签页），开发者邮箱仍是纯文本。当时仓库还是私有的（匿名请求被 Gitee 拒，403），公开后仍报 404 的分支名问题见下方「修复」小节。`livedeck/static` 缓存版本串递增到 `20261110g`。新增 `tests/test_update_check.py`（16 例），目录共 418 例。详见 [docs/12](docs/12-Web管理台.md) §22.10 与 [docs/23](docs/23-多屏直播监控台.md) §2。
### 变更

- **测试文件退出发布线与发布包，发布线历史重写**（2026-11-10）：按用户要求，Gitee 上不再能看到 `tests/`（41 个文件，含 `tests/fixtures/` 的真实动态样本）与 `scripts/selftest_*.py`（15 个手工自测脚本）。三处一起收拾：(1) `opensource` 发布线的历史提交全部重写（保留原提交信息、作者与时间），重写后的树不含测试文件，force push 覆盖；(2) annotated tag `v0.1.0` / `v0.3.0` 重指到重写后的提交（`AGENTS.md` R6 的「tag 不可改」这次按用户明确要求放行，属一次性破坏性操作）；(3) `scripts/release.py` 的 `EXCLUDE_DIRS` / `EXCLUDE_GLOBS` 把 `tests` 与 `selftest_*.py` 排除，发行版附件 `bili-monitor-0.3.0.zip` 按原包剔测试重打（734 → 679 个条目）后覆盖上传。开发线 `master` 保持测试全量、发版前照旧全量跑通；同步流程见 [docs/34](docs/34-开源发布清单.md) §9。
- 历史发行版曾通过 Gitee 分发。
- **README 精简，索引与代码地图迁到 docs/37**（2026-11-10）：README 从 272 行压到 129 行，只留「这是什么 / 五分钟上手 / 常用命令 / 目录结构 / 文档入口 / 版本与发布 / 安全基线 / 贡献 / 许可」；原来的 29 行文档索引与 95 行逐文件代码地图原样移到 `docs/37-文档与代码索引.md`（相对链接改成 docs/ 视角，123 个链接全部可解析）。`AGENTS.md` §9 与 `CONTRIBUTING.md` 的「代码地图」入口同步指向新文件；删掉过时的「CI 也会跑同一条链路」（Gitee 不执行 GitHub Actions），安全基线由 8 条长文压成 5 条并链到 `docs/30`。

### 修复

- **弹幕引擎离线：Edge profile 目录 ACL 不全，浏览器静默退出**（2026-11-10）：监控室 `/api/snapshot` 里 `bridge.status=error`、`error="BridgeError: DevTools 未就绪：<urlopen error timed out>（退出码 0）"`，`.research/run.log` 只有这一句，`edge.log` 是空的。根因在 `livedeck/bridge.py` 的 `_launch()`：它用 `tempfile.mkdtemp()` 建 Edge profile 目录，Python 在 Windows 上按 0o700 建，落成的 ACL 只有「OWNER RIGHTS + SYSTEM + Administrators」，**没有当前用户那条 ACE**；Edge 的受限令牌在启动阶段写不了 profile，启动器静默退出（退出码 0），DevTools 端口永远不应答。对照实验同机同参数：`os.makedirs()` 建的目录连跑 3 次都能起，`mkdtemp` 连跑 3 次全失败；`os.chmod(0o777)` 补救无效（chmod 不补 ACE）。现在新增 `bridge._make_profile_dir()`，用 `os.makedirs` + 随机后缀建目录（重名重试），`tests/test_livedeck_bridge.py` 加 2 例守住「源码里不再出现 `= tempfile.mkdtemp(`」与「新目录可写」，目录共 422 例。详见 [docs/23](docs/23-多屏直播监控台.md) §5.1 第 8 条。
- **「更新检查」不再报 `HTTP 404`：改跟发布线 `opensource` 比对**（2026-11-10）：仓库公开后点「检查失败：HTTP 404」——`shared/update.py` 原来只试 `raw/master/VERSION` 与 `raw/main/VERSION`，而 Gitee 仓库的默认分支是 `opensource`（`master` 不推发布线，`main` 不存在），两个都 404。现在 `REFS = ("opensource", "master", "main")` 按序试，**`opensource` 排第一**（发布线只收整批确认的快照，`master` 可能带着未发布改动，跟「已发布的版本」比才对）；404 的文案也从裸 `HTTP 404` 改成「仓库里没有 VERSION（HTTP 404，试过 opensource / master / main）」，能分清「分支改名」和「文件丢了」。实测 `GET /api/update` → `{"ok": true, "current": "0.3.0", "latest": "0.3.0", "has_update": false}`。`tests/test_update_check.py` 相应改为 17 例（新增「发布线优先」断言，回退与全失败用例按三条 URL 重写），目录共 419 例。

## [0.3.0] - 2026-11-10 — 发布形态改造：自带 Python 运行时（绿色包）

第二位递增（minor）：多个重要改动合并 —— 绿色包、安装流程重写、启动脚本与作业判定适配。

### 新增

- **进程页主进程可重启 / 停止，停止两步确认**（2026-11-10）：进程页「监控台主进程」那行原来只有「只读」，现在给两个动作。新增 `POST /api/tasks/main`（body `{action: "restart"|"stop", confirm?}`）：**托管**时（`data/supervisor.json` 里的守护活着）重启写 `data/restart.request`、停止写 `data/supervisor.stop`（主进程、日程进程组、守护一起优雅退出）；**独立运行**时重启交给 `scripts/restart_console.ps1`，`run_bot.py` 停止写 `data/bot.stop`、单独跑的管理台延迟 0.8 秒关掉 HTTP 服务器。停止不可撤销，接口要求 `confirm: true`，页面走两步：第一次点把按钮换成「确认停止 / 取消」（15 秒过期；表格每 3 秒整表重绘，确认态存在全局所以不会被冲掉），第二次点才真发请求；停止成功后停掉 3 秒轮询并显示「监控台已停止」与恢复命令，重启则显示过渡卡片并自动重试。`GET /api/tasks` 的 self 行新增 `managed`（是否托管：守护在跑**且**本进程或其父启动器就是它拉起的那个，另开的临时管理台不会误判）与真实 `port`（由 `make_server()` 注入，不再写死 8765）。新增 `tests/test_tasks_main_control.py`（14 例），目录共 402 例。详见 [docs/12](docs/12-Web管理台.md) §22.9。
- **自带 Python 运行时：发布包变成「绿色包」**（2026-11-10）：包内新增 `runtime/`（CPython 3.12.7 embeddable x64，约 45 MB），依赖预装进 `runtime/Lib/site-packages`，目标机器**不装 Python、不联网**也能跑。新增 `scripts/build_runtime.py`：下载官方 embeddable → 解压 → 改写 `python312._pth`（放开 `Lib\site-packages` 并取消 `import site` 注释）→ `pip --target` 装 requirements.txt + requirements-optional.txt → 冒烟 import 五个必需依赖；`--check` 只校验现有 runtime，`--force` 重建。
- **`preflight.py` 认识自带运行时**：新增 `python_kind(root, exe)`，`runtime` 判 OK「自带运行时」、`.venv` 判 OK、系统 Python 判 WARN（提示用包内解释器）；报告键由 `python.venv` 改为 `python.env`。
- **`release.py` 能打绿色包**：`should_pack()` 对 `runtime/` 整目录放行（`python312.zip` 是标准库本体，原会被通用 `*.zip` 规则误杀），只排除 `.pyc`/`__pycache__`；新增 `check_runtime()` 校验 6 个关键文件（含 `runtime/LICENSE.txt` = PSF 许可证）与 `_pth` 的 site-packages / `import site`；没有 runtime 时 check 只报警告、build 直接拒绝。
- **CI 增加绿色包作业**：`.github/workflows/ci.yml` 新增 `runtime-package`——setup-python 3.12 → `build_runtime.py` → `--check` → `release.py check` → `build` → 解压后**只用包内** `runtime\python.exe` 跑 `preflight.py` 与 `run_bot.py --once --channel echo`，验证「解压即用」。
- **测试守运行时契约**：新增 `tests/test_runtime_bundle.py`（20 例），覆盖 `_pth` 内容、`build_runtime.check()`（缺 python312.zip / 缺 site-packages / `import site` 被注释都要判死）、`release.should_pack`（runtime 整目录进包、`.pyc` 不进包）、`release.check_runtime`、`preflight.python_kind`。
- **「关于」弹窗与 `GET /api/version`**（2026-11-10）：两侧设置菜单里的**关于**从灰显改成真正可点，打开同一套「关于」弹窗——监控室 `#about-mask`、监控台 `#aboutModal`，文案一致：标题「DD全能工作间」+「版本 vX.Y.Z · MIT 许可」、一句话说明、三条要点（数据只在本机 / 只监听环回 / 非官方工具）、开发者三行（邮箱、源码、反馈都是**纯文本不可点**）、页脚写平台与许可。版本号**不在前端写死**（AGENTS §1 R5），打开时向新路由 `GET /api/version` 取：`livedeck/server.py` 与 `web/console_views.py` 都转调 `shared.version.read_version()`，唯一来源仍是 `VERSION`；console 侧按约定同步了数据层与 `console_http.py` 分派。关闭走右上角 ×、点遮罩空白处、Esc（与既有弹窗共用一条 keydown 链，先关关于再收设置菜单）。「更新检查」保持灰显。`livedeck/static` 两个文件缓存版本串递增到 `20261110d`。新增 `tests/test_about_dialog.py`（21 例），两侧设置菜单测试各扩 1 例，目录共 358 例。详见 [docs/12](docs/12-Web管理台.md) §22.8 与 [docs/23](docs/23-多屏直播监控台.md) §2。
- **监控台顶栏「设置」菜单**（2026-11-10）：Web 管理台（8765）右上角新增齿轮按钮（`#settings-btn`），点开三项菜单：**监控室**在新标签页打开多屏直播台 `http://127.0.0.1:8760/`；**更新检查**与**关于**暂未做，灰显标「暂不可用」，点击只弹一条提示，等后续任务。同时**取消了导航栏原来单独的那条「监控室 ↗」链接**（连 `nav a.navlink` 样式一起删掉），入口只剩菜单里那一项，避免两个入口并列。菜单支持点空白处与 Esc 关闭，按钮带 `aria-haspopup` / `aria-expanded` / `aria-controls` 与 `role="menu"`，和监控室那边的设置菜单对称。新增 `tests/test_web_settings_menu.py`（6 例），目录共 332 例。详见 [docs/12](docs/12-Web管理台.md) §22.7。
- **监控室顶栏「设置」菜单**（2026-11-10）：多屏直播台右上角新增齿轮按钮（`#settings-btn`），点开四项菜单：**监控台**在新标签页打开 Web 管理台 `http://127.0.0.1:8765/`；**使用说明**复用现有 `#help-mask` 说明弹窗；**更新检查**与**关于**暂未做，灰显标「暂不可用」，点击只弹一条提示，等后续任务。菜单支持点空白处与 Esc 关闭，按钮带 `aria-haspopup` / `aria-expanded` / `aria-controls` 与 `role="menu"`。同时**移除了顶栏原来单独的 `?` 帮助按钮**（说明入口并入菜单，`#help-mask` 弹窗内容不变）。`livedeck/static` 两个文件缓存版本串一并递增到 `20261110b`。新增 `tests/test_livedeck_settings_menu.py`（6 例），目录共 326 例。详见 [docs/23](docs/23-多屏直播监控台.md) §2。
- **上舰在弹幕流里按等级做视觉区分**（2026-11-10）：舰长 / 提督 / 总督 不再只是一行「昵称 舰长」纯文本。`hub._handle_guard()` 把帧里的 `guard_level`（1=总督、2=提督、3=舰长）和购买月数带进消息的 `extra`，`gift_name` 缺失时按档位兜底；前端整行按档染色（金 / 紫 / 蓝渐变底 + 描边）、行首放 B 站大航海的锚形图标（`livedeck/static/img/guard1~3.png`，1 金=总督 / 2 紫=提督 / 3 蓝=舰长，18px，B 站大航海三档官方图标，蓝=舰长、紫=提督、红金=总督）、文字改成「昵称 开通了提督 ×3 个月」，并带一段 0.32 秒入场动画。三档样式是 `style.css` 的 `.m.guard.g1~g3`，徽标是 `.m.guard .gl`；`tests/test_livedeck_guards.py` 从 6 例增至 9 例（等级 / 月数 / 名称兜底 / 页面契约）。详见 [docs/23](docs/23-多屏直播监控台.md) §4.6。
- **左栏分组可拖动排序**（2026-11-09）：监控室左栏的分组头现在可以按住上下拖动调整顺序，松手即保存，重启后保持；「临时」组固定排最后，没排过的新分组按首次出现顺序补在后面。顺序只记**分组名**（不记房间、不记折叠状态），存 `data/livedeck_state.json` 的 `group_order`，服务端 `DeckRooms.set_group_order()` 负责去空 / 去重 / 40 字符截断 / 最多 100 个，新增 `POST /api/groups/order` 与 `/api/snapshot.group_order`；前端拖动时给目标组画粉色插入线，并用 `st.suppressGrpClick` 吃掉拖完补发的那次 click，避免误把分组收起。新增 `tests/test_livedeck_group_order.py`（9 例）。详见 [docs/23](docs/23-多屏直播监控台.md) §4.8。
- **房间印象色：弹幕标签按主 / 辅色相种子取色**（2026-11-09）：房间操作菜单新增「设置印象色」，可以给单个房间指定主色 + 辅色两个色相种子（色盘 / 屏幕取色 `EyeDropper` / `#rgb`、`#rrggbb` / R-G-B 数字输入，四种方式互相回填）；开启印象色时主色必填、辅色选填，支持取消与「清除印象色」。种子只提供色相，字色亮度与底色透明度由代码按主题固定以保证对比度：两色可合理兼容（都是彩色且色相差 ≥ 24°）用双色渐变底，黑 + 白这类无彩色组合（熊猫色，明度差 ≥ 0.35）用中性双色底，其余情况**只按主色**。没设种子的房间仍走原来的 `hue(id)` 哈希规则，房间列表 / 二次菜单 / 格子头部都不染色，种子只作用于右栏弹幕流标签，保存后 `repaintTags()` 立即重刷已上屏的行。后端 `deck_base.normalize_color()` 归一颜色（认 `#abc` / `#aabbcc` / `rgb(r,g,b)` / `r,g,b`），`DeckRooms.set_room_color()` / `colors_payload()` 落 `data/livedeck_state.json` 的 `colors`，新增 `POST /api/room/color` 与 `/api/snapshot.colors`。新增 `tests/test_livedeck_colors.py`（10 例）。详见 [docs/23](docs/23-多屏直播监控台.md) §4.7。
- **监控室显示大航海人数**（2026-11-09）：舞台格子头部新增「舰 N」徽标（0 人或未开播时隐藏）。弹幕流里只有 `GUARD_BUY` 的增量和每个人的 `guard_level`，**没有总数**，所以总数走 `getInfoByRoom` 的 `data.guard_info.count`（舰长 + 提督 + 总督，与房间页「大航海(N)」同源）；这个接口本来就在 `DeckInfo._watched_info()` 里按房每 20 秒拉一次拿「看过 / 人气」，同一次响应顺带取数、不额外发请求。`/api/snapshot` 的 `info[room].guards` 与 `bridge.rooms[room].guards` 各暴露一份。新增 `tests/test_livedeck_guards.py`（6 例）。详见 [docs/23](docs/23-多屏直播监控台.md) §4.6。
- **管理台主进程重启脚本**（2026-11-09）：新增 `scripts\restart_console.ps1`，只重启 127.0.0.1:8765 的管理台进程。在守护下面时写 `data/restart.request` 走优雅重启（守护自己不重启、不计崩溃次数），独立进程则记下原命令行后用 pythonw 重新拉起、输出进 `logs/console.log`；`-Status` 只看端口/守护/进程链/HTTP，`-Force` 直接杀监听进程交给守护按崩溃路径拉起，`-Start` 在没跑时单起管理台。新增 `tests/test_restart_console.py`（6 例）。详见 [docs/13](docs/13-守护与开机自启.md) §9.4 与 [docs/12](docs/12-Web管理台.md) §11。
- **监控室「只听弹幕」**（2026-11-08）：多屏直播台可以在不把直播间放进舞台的情况下只接收该房间的弹幕流。房间不占 16 路舞台格子、不拉 HTTP-FLV，只加入弹幕桥订阅并出现在右栏弹幕流；左栏每行新增「听」按钮、底部新增「只听」，弹幕流下拉新增「只听弹幕」分组。后端新增 `LiveDeck.listening`（持久化到 `livedeck_state.json`）、`_watched_rooms()` 与 `POST /api/listen`，`/api/history` 与 `/api/stream` 默认覆盖舞台 + 监听房，`/api/flv` 仍只认舞台；监听上限 16 路，房间上舞台会自动退出只听。新增 `tests/test_livedeck_listen.py`（13 例），目录共 213 例。
- **日程功能默认关闭，隐藏入口 + 关键字开关**（2026-11-08）：新装或既有安装都默认不跑日程相关的四条循环，
  管理台隐藏「日程表」页签、监控名单的「日程」列与进程页的日程进程组。在「监控名单」的添加框输入
  `weekly_open` 点「添加」即开启并让守护进程拉起日程进程组；输入 `weekly_close` 关闭并收起进程组。
  开关存 `meta['weekly_feature_enabled']`，关闭时同时把四个任务写进 `meta['tasks_off']`；管理台新增
  `GET/POST /api/weekly/feature`，守护进程每轮按开关拉起/停止进程组，进程组自身每 5 秒检查一次并自行退出。
  新增 `tests/test_weekly_feature.py`（8 例），目录共 200 例。详见 [docs/12](docs/12-Web管理台.md) §24 与
  [docs/33](docs/33-AI日程识别.md) §1。
- **日程进程组独立成进程**（2026-11-08）：周表扫描、直播预约刷新、日程识别交接、每日推送四条循环从
  `scripts/run_bot.py` 主进程移出，改由新进程 `scripts/schedule_worker.py` 常驻执行；每日推送的纯逻辑抽到
  `core/weekly/push.py`。`scripts/supervisor.py` 现在同时拉起主进程与日程进程组，各自崩溃重启、各自心跳
  （主进程看 `meta.last_tick`，进程组看 `meta.worker_state.last_beat`），停止/重启时两个一起收，`--no-worker`
  可只跑主进程。管理台「进程」页新增「日程进程组（独立进程）」分组：进程组本身与四个循环都只读，暂停仍按
  `meta['tasks_off']` 生效；`service.ps1 status` 显示 `worker_pid`，`stop` 同时写 `data/schedule_worker.stop`。
  新增 `tests/test_schedule_worker.py`（10 例），目录共 192 例。详见 [docs/13](docs/13-守护与开机自启.md)、
  [docs/12](docs/12-Web管理台.md) §22。

### 变更

- **删除 `docs/03-部署教学手册.md`**（2026-11-10）：那份 Docker / WSL / NAS 路线与 NapCat 附录是早期设计稿，与现在的绿色包 + 官方 QQ 机器人私聊链路不符，容易误导接手的人；Windows 安装与接入统一以 [docs/31](docs/31-发布与交接手册.md) 为准。原文仍留在 git 历史里（`git log -- docs/03-部署教学手册.md`），需要时可取回。
- **安装从「装依赖」变成「校验 + 生成配置」**（2026-11-10）：`scripts/install.ps1` 重写为 4 步——校验 `runtime\python.exe` 与五个必需依赖 → 从 `*.example` 生成配置骨架（不覆盖已有）→ 建 `data/ logs/ out/ dist/` 并收紧凭据 ACL → 跑 `preflight.py`；**全程不联网**。`-Python` / `-NoVenv` / `-Mirror` / `-Force` 仍接受但只提示已忽略。没有 `runtime/` 时退回开发机 `.venv`，两者都缺则报错并提示重新解压或重建运行时。
- **启动脚本一律「runtime 优先」**：`scripts/service_start.cmd`、`scripts/service_hidden.vbs`、`scripts/service.ps1`、`scripts/restart_console.ps1`、`scripts/install_cleanup_autostart.ps1`、`start_livedeck.bat` 按 `runtime\pythonw.exe`/`runtime\python.exe` → `.venv` → PATH 的顺序找解释器，不再依赖任何本机路径。
- **`service.ps1 status` 的作业对象判定按运行时语义改写**：`runtime` 里的 `python.exe` 就是解释器本体（不是 venv 启动器），进程树只有两层，WMI 创建的子进程不在任何作业里；保留 `Test-VenvLauncher` 分支给开发机 `.venv`（那才会出现「启动器自建作业」的四层树）。实测见 [docs/13](docs/13-守护与开机自启.md) §10.4.1。
- **依赖清单语义变了**：`requirements.txt` 现在是**构建期**清单（固定版本，由 `build_runtime.py` 预装进运行时），终端用户什么都不用装；文件继续保持纯 ASCII（pip 在中文 Windows 上按本地编码读它）。
- **`runtime/` 不入库**：加入 `.gitignore`（约 45 MB / 818 个文件），由构建脚本生成、随 zip 分发。
- **品牌统一为「DD全能工作间」，版权主体改为 VictorKiseki**（2026-11-10）：产品名不再用旧的英文品牌名，界面名沿用「监控台」（`:8765`）/「监控室」（`:8760`）。监控室顶栏 logo 改为 `DD` + 粉色「全能工作间」，`<title>` 两边分别是「DD全能工作间 · 多屏监控室」「DD全能工作间 · 监控台」，管理台标题行改为「DD全能工作间 / 监控台」。`LICENSE` 版权行与两侧「关于」弹窗页脚统一为 `© 2026 VictorKiseki`。代码注释、启动日志与 `/` 的 `Server` 头一并换名——`server_version` 由写死的旧品牌串 `…/0.1` 改成 `"DDWorkshop/" + read_version()`，版本号仍只来自 `VERSION`。`livedeck/static` 缓存版本串递增到 `20261110f`。新增 `tests/test_brand_naming.py`（10 例）：全仓扫描旧品牌名与旧版权主体（拆开拼接，避免自匹配），并锁定两侧标题、logo、页脚与 `server_version`。
- **监控名单取消启用 / 停用**（2026-11-10）：名单页不再有单行的「开关」列与批量的「启用 / 停用」按钮，`PATCH /api/subjects/{uid}` 与批量 `action=enable|disable` 一并删除（现在分别返回 404 / 400），所有主播一律参与轮询；历史上被停用的号由 `schema v5` 迁移抬回 `enabled=1`（水位、推送开关、直播会话都保留），镜像 `config/subjects.json` 不再写 `_disabled`。此后 `enabled` 只表示「是否在名单里」，由启动对账维护（见 [docs/35](docs/35-关注流采集.md) §6.1）；想静音单个号，用推送目标里的「直播 / 动态 / 周表」开关。新增 `tests/test_subject_no_disable.py`（8 例），并同步 `tests/test_uid_range.py` 的 PATCH / 批量断言。
- **每日日程推送时间可在管理台改**（2026-11-10）：日程表页顶部新增「每日推送」卡片（`#wkPushAt` + `#wkPushSave`），把时间存进 `meta['daily_push_at']`，不用再改 `BBOT.env` 重启进程。读取顺序统一为库 > `BBOT_PUSH_AT` > 默认 `08:10`，`get_push_at()` 是唯一读口；`push_loop()` 改成 60 秒轮询配置，改完 1 分钟内生效，当天已推过不会重推，改成今天已经过去的钟点则从明天开始（`--push-now` 可立即补推）。`scripts/push_alert.py` 的漏推判断与推送循环同口径，改过时间后不会还拿旧的 08:10 报 `missed`。`POST /api/weekly/feature` 现在接受 `enabled` 和/或 `push_at`，`GET` 多返回 `push_at` / `push_at_default`；`set_push_at()` 拒绝空值与 `off`，要停推送请关总开关或暂停 `daily_push`。新增 `tests/test_schedule_worker.py` 的 `PushTimeTests` 与 `tests/test_weekly_feature.py` 的 `test_push_time_status_and_update`，目录共 308 例。详见 [docs/21](docs/21-周表动态识别可行性分析.md) §35 与 [docs/12](docs/12-Web管理台.md) §24。
- **日程表正文不再重复「今天 / 明天」**（2026-11-10）：条目已经落到具体日期后，正文里的相对日词就是冗余信息，展示时去掉，库里仍保留原文。`core/weekly/parse.py` 新增 `strip_day_words`，`entry_body_text` 对正文和待定时追加的 `time_text` 都先过一遍；管理台 `web/index.html` 新增同口径的 `wkStripDay` 并接进 `wkBody`。只删「今天 / 今日 / 今晚 / 明天 / 明晚 / 后天 / 大后天」这类相对日，**不动「周三」等星期词，也不动「明日方舟」这类作品名**；只讲日期的括号片段整段删（`周三见面（今天、明天休息）` → `周三见面`），删完只剩标点也清掉（`今天、明天休息` → `休息`）。`tests/test_weekly_time_display.py` 新增 4 例（共 15 例），目录共 302 例；`scripts/selftest_weekly.py` 272 ok。
- **日程表时间只写 H:mm，休息 / 待定分开展示**（2026-11-09）：具体钟点统一显示 `H:mm`（`09:30` → `9:30`），区间取起点（`9~12` → `9:00`、`晚上9~12` → `21:00`），中文数字换算成 24 小时制（`晚上十点` → `22:00`、`十一点半` → `11:30`）；说不出钟点的写「待定」并把原词并进正文（`歌杂（晚上）`），整条休息写「休息」；周表里只写了 `note="休"` 的日子也会生成休息条目，不再是一片空白。`core/weekly/parse.py` 的 `normalize_time` 支持区间与中文数字，并新增 `format_hm` / `is_rest` / `is_fuzzy_time` / `entry_time_label` / `entry_body_text`；`core/weekly/view.py`、`web/index.html`、`scripts/daily_post.py`、`scripts/card_pil.py` 与 `batch.py` 的 `TIME_HINT` 同步。新增 `tests/test_weekly_time_display.py`（11 例），目录共 242 例。
- **临时房间「加入监控台名单」移入操作菜单**（2026-11-09）：移除直播间弹窗不再展示「加入监控台名单」，改到左栏临时房间的三横线操作菜单；名单内房间菜单仍显示「设置分组」，临时房间点「加入监控台名单」后写入 `data/bot.db` 并刷新，随后即可设分组/停用/删除。移除弹窗只保留隐藏与从列表删除。新增 `test_temp_room_add_option_moved_into_menu`，目录共 231 例。
- **重点观察尺寸不再跨舞台保留**（2026-11-09）：`▣` 放大的 2×2 / 3×3 只记在当前页面，不再读写 `localStorage['ld.zoom']`；房间每次重新进舞台都从 1×1 开始，刷新页面也全部还原，旧版本留下的尺寸在启动时清掉。新增 `tests/test_livedeck_zoom.py`（2 例），目录共 230 例。
- **监控室左栏不再显示每房弹幕条数**（2026-11-08）：左栏房间行只保留房间号、分组、直播状态与「只听弹幕」标记，去掉「N 条弹幕」；顶栏的弹幕/分与已接收条数、右栏计数不受影响。同步 `tests/test_livedeck_rail_menu.py`，目录共 220 例。
- **监控室组合支持只听弹幕 + 左栏菜单手感**（2026-11-08）：临时组合的勾选列表现在同时列出「在舞台」与「只听弹幕」两组，只收弹幕、不上舞台的房间也能并进同一条弹幕流；每组最多 32 路（两边名额相加），下拉与面板的「可收路数」把两类房间都算进去。左栏每行的「操作」入口改为三横线图标（保留 `aria-label`），二次菜单不再因为右侧弹幕流滚动而收起，只在左栏自身滚动、点空白、Esc 或窗口缩放时关闭。新增 `tests/test_livedeck_combo_listen.py`（2 例）并扩充左栏菜单测试，目录共 219 例。
- **监控室左栏房间操作收进二次菜单**（2026-11-08）：每行原来平铺 `+` / `听` / `#` / `×` 四个小图标，
  现在只留一个「操作」按钮，点开文字菜单：加入 / 移出舞台、只听弹幕 / 停止只听、设置分组（仅名单内）、
  移除直播间；菜单贴着按钮展开，视口底部会自动上翻，点空白、滚动或 Esc 收起。底部新增房间的「加入 / 只听」
  两个按钮改为不收缩、文字不换行，输入框允许压缩，窄栏下也不会把按钮挤成两行。新增
  `tests/test_livedeck_rail_menu.py`（2 例），目录共 216 例。
- **日程表页移除 AI 识别前端入口**（2026-11-08）：管理台「日程表」页不再显示顶部的「定时识别」条与「日程识别模型」卡，
  `web/index.html` 也不再请求 `/api/weekly/ai` 与 `/api/weekly/agent`；待确认候选改为纯人工定性（看图点「周表 / 单场 / 非日程」）。
- **日程识别改为 Harness 交接，移除产品内模型接入**（2026-11-08）：删掉 `shared/ai.py`、`config/ai.env.example`、`tests/test_ai.py`
  以及模型配置 / 连通性测试 / 立即识别的路由与命令，安装不再生成 `config/ai.env`。产品只保留：闸门（开关 / 时段 / 条数 / 人工排队）、
  导出 `out/ai_tasks/<batch_id>.json`、回收 `out/ai_results/*.json` 并入库；读图交给本机 DSH 会话（手动或定时自动化），
  进程本身不发任何模型请求。`weekly_agent_loop` 每轮先回收、到时段才导出（人工排队可立即导出）；
  `scripts/weekly_ai.py` 的 `run` 子命令删除，`scripts/weekly_agent.py` 只剩 `status` / `export` / `ingest`。
  详见 [docs/33](docs/33-AI日程识别.md) 与 [docs/12](docs/12-Web管理台.md) §24。

### 修复

- **`build_runtime.py --check` 漏判注释掉的 `import site`**：原来是子串判断，`#import site` 也算通过；改为按整行正则匹配（`^\s*import\s+site\s*$`）。
- **`install.ps1` 在 PowerShell 5.1 下误报「运行时跑不起来」**：探针里的双引号会被 5.1 从原生参数里剥掉，Python 收到碎语法直接报错；改成不含任何引号的探针，只判退出码。
- **重写后丢 UTF-8 BOM 的两个 `.ps1`**：`scripts/restart_console.ps1`、`scripts/install_cleanup_autostart.ps1` 重建时丢了 BOM，PS 5.1 会把中文尾字节当语法错；已补回，并继续由 `release.py check` 兜底。
- **关注了但不在监控名单里的主播仍在推送**（2026-11-10）：管理台以库为权威、`config/subjects.json` 只是镜像，绕过管理台直接进库的历史行在库里 `enabled=1`，关注流按库名单过滤就当成「名单内」，照常产事件、照常推送。现在 ① `Watcher.init_subjects` 启动时**双向对账**：名单内一律启用、名单外一律用新增的 `Store.set_subject_enabled()` 停用（不删除，水位与推送开关都留着，加回名单下一次启动就恢复），日志打 `[名单] 停用 N 个不在监控名单里的号：名字(uid)` / `[名单] 恢复 M 个重新加入名单的号：…`；② 关注流里名单外的条目只计入 `feed_last_ignored`（管理台取流行显示「名单外忽略 N 条」），不落库、不推送；③ `load_subjects` 不再读镜像里的 `_disabled`——管理台 2026-11-10 已取消手动启用 / 停用，`enabled` 只表示「是否在名单里」。`store/sqlite.py` / `store/memory.py` 各加 `set_subject_enabled()`，`shared/contracts/store.py` 协议同步；④ `_sync_config_subjects` 只把 `enabled=1` 的行写回镜像——否则「停用 → 镜像带回 33 条 → 下次启动当成名单 → 又启用」会在每次重启时回环（2026-11-10 实测过一次）。`tests/test_feed_watcher.py` 的 `SubjectListTests` 增 3 例（停用、恢复、名单外条目不产事件）。详见 [docs/35](docs/35-关注流采集.md) §6.1。
- **印象色「吸色」只能取到模糊后的画面颜色**（2026-11-09）：设置弹窗的遮罩带 `backdrop-filter: blur(6px)` 和 72% 压暗，
  `EyeDropper` 取的是屏幕上**真实渲染**的像素，于是直播间画面被糊成一片、还蒙一层黑，取到的颜色跟肉眼看到的对不上。
  现在点「吸色」时给 `body` 加 `picking` 类：遮罩改全透明、去掉 blur、弹窗本身 `visibility:hidden`，顶部留一句
  「吸色中：对着直播画面点一下取色 · Esc 取消」；取到色或按 Esc 取消（`catch` 分支同样撤销）后遮罩原样恢复。
  取色器开在系统层、不触发 `visibilitychange`，也就不会踩到「后台暂停」把画面停住。相关 CSS 收在
  `livedeck/static/style.css`（`body.picking .mask` / `body.picking .pick-tip`），流程在 `app.js` 的 `pickScreen()`；
  `tests/test_livedeck_colors.py` 增加对应契约断言。详见 [docs/23](docs/23-多屏直播监控台.md) §4.7。
- **16 位 UID 的主播在名单里任何操作都报「没有这个 UID」**（2026-11-09）：管理台解析 UID 走
  `safe_int(hi=10**12)`，而 `safe_int` 是**夹取**语义——`3461582034045213`（< 2^53，前端 JS 精度不受影响）
  被静默改成 `1000000000000`，再按 1e12 查库自然查不到，开关推送 / 立即抓取 / 移除全部 404。
  现在新增 `webutil.safe_uid()` 与 `MAX_UID`（只校验 `[1, 2**63-1]`，越界 400「不是合法 UID」，绝不改写原值），
  `web/console_http.py` 的 `_uid()` 统一收口 6 处 UID 解析（`GET /api/events`、`POST /api/subjects`、
  `POST /api/subjects/flags`、`PATCH|DELETE /api/subjects/<uid>`），`web/console_weekly.py` 的人工日程 UID
  同步改用 `safe_uid`，`set_subject_flags` 缺 UID 时直接 400 而不是 404。节目 / 日程 / 目标 ID 的 `10**12`
  上限与 UID 无关，保持原样。新增 `tests/test_uid_range.py`（12 例，覆盖 16 位 UID 的 HTTP 分派与真实
  SQLite 表操作），目录共 286 例。
- **弹幕桥已连通却收不到弹幕（2026-11-09）**：认证包把 `protover` 写死成 3，而 `.venv` 默认没装 brotli（可选依赖），服务器按 3 回的帧在 `decode_packet` 解压那一步被整包丢弃，`/api/diag` 里只剩 `STOP_LIVE_ROOM_LIST` / `NOTICE_MSG` 这类偶尔以明文发的广播、`count` 恒为 0。现在 `livedeck/hub.py` 用 `HAS_BROTLI` / `protover_for_server()` 按本机能力选认证版本（没 brotli 报 2/zlib），`decode_body()` 在声明版本解不开时按另一种压缩兜底，解不开的帧记进 `DECODE_STATS` 并暴露到 `/api/diag` 的 `decode`（`brotli` / `protover` / `dropped`）；启动日志会打一条「brotli 未安装：弹幕认证走 protover=2」。页面侧认证包改为 `it.protover || 3`，`deck_bridge._bridge_items()` 给每房下发 `protover`。新增 `tests/test_livedeck_decode.py`（10 例）。详见 [docs/23](docs/23-多屏直播监控台.md) §5.1。
- **没开日程监控的主播不再进日程表**（2026-11-09）：直播预约刷新只查 `subject.enabled=1`，导致 `weekly_on=0` 的主播（如花礼Harei）的预约仍被写成 `weekly_session` 并出现在日程表。`refresh_reserves` 与 `ingest_reserve` 现在都按 `weekly_off_uids` 拦截（返回 skipped、不写库），`week_view` 也不再展示这类主播的历史 session；正在开播但原表没有的场次本来就不会被 `annotate_live` 补入。新测试同上。
- **日志级别误判与轮次类型拆分**（2026-11-08）：`[直播预约] 新增/更新 N 条，跳过 N，错误 0` 里的「错误 0」被关键字判成 error，导致预约日志几乎全红；`core/logfmt.py` 现在先剔除「错误 / 失败 / 异常… 0」这类零值计数再判级，真实 `错误 N`（N>0）仍是 error，「跳过」也不再算警告；预约汇总只在非零时追加跳过 / 错误计数。主循环的 `[轮次 419]` 统一归一为 `主循环轮次`（日志改为 `[主循环轮次] 第 419 轮 …`），历史日志的类型筛选与统计合并到同一类。新增 `tests/test_logfmt.py`（6 例），`selftest_logs.py` 增到 30 条断言，目录共 228 例。
- **后台子进程不再弹控制台窗口**（2026-11-08）：`pythonw` 守护、日程进程组和监控室启动 `powershell.exe`、`taskkill`、`netstat` 时统一传 `CREATE_NO_WINDOW`（新增 `shared/proc.py` 的 `no_window_flags()`），覆盖周表 OCR、每日推送、弹幕桥进程清理、进程页探测与登录自启安装；稳定运行时不再出现终端闪窗。新增 `tests/test_no_console_window.py`（2 例），目录共 222 例。
- **弹幕桥在管理员权限下 CDP 假死**（2026-11-08）：管理台/守护进程以管理员身份启动 livedeck 时，Edge 157 的渲染器沙箱会让 headless 页面 target 假死——浏览器 WS 能连、`Target.getTargets` 正常，但 `Page.enable` / `Runtime.evaluate` 一律超时，日志反复出现 `BridgeError: CDP 超时：Page.enable`。已试过换 headless 模式、单进程、SwiftShader、关 GPU/WebGL、换 profile 目录等均无效，仅 `--no-sandbox` 可恢复；`livedeck/bridge.py` 的 `FLAGS` 现已固定带该开关（临时实例只访问 B 站）。新增 `tests/test_livedeck_bridge.py`（1 例），目录共 214 例。
- **卡片信息不完整时自动重取**（2026-11-08）：开播卡头像空白 / 名字显示成 `UIDxxx`、图文动态卡只有图没有正文，
  根因是采集那一刻数据就不全（批量直播接口偶发漏 `uname`/`face`；desktop 兜底端点没有正文与图片宽高）。
  现在在产事件之前定向重取：动态走 `/x/polymer/web-dynamic/v1/detail?features=itemOpusStyle`（失败再翻 space），
  直播走单房间 `get_info`；每条最多 2 次、每轮最多 5 条（`poll.enrich_retries` / `poll.enrich_max_items`），
  **补不齐也照常推送**，只记一条 `[补全] … 仍缺 …` 警告。卡片侧另加本地头像兜底 `data/avatars/<uid>.jpg`。
  详见 [docs/36](docs/36-信息补全与自动重取.md)。
- **`service.ps1 status` 误报「在作业对象里」**：Windows 上 `.venv\Scripts\python*.exe` 是 CPython 的启动器，
  它会自建作业把真解释器套进去，导致 `IsProcessInJob(…, NULL)` 永远答「在」。现在会再往上看一层，
  外层启动器不在任何作业里就判 OK 并说明来历（见 docs/13 的 10.4.1）。

### 升级提示

- 这一步之后**目标机器不再需要 Python**：把新 zip 解压到新目录，只迁移 `BBOT.env`、`config\*.env`、`config\*.json`、`.p0_ids.json`、`data\bot.db*`，然后双击 `install.cmd`。
- 旧目录里的 `.venv` 可以直接删；开发机若想继续用虚拟环境，`install.ps1` 在没有 `runtime/` 时会退回它。
- 包体积从约 1 MB 涨到约 20 MB（zip）/ 约 50 MB（解压后），这是内置 CPython 与预装依赖的代价；换来的是对方机器上零前置条件。
- 已有读者注意：`runtime/` 由 `python scripts/build_runtime.py` 生成，仓库里没有它；从源码跑请参考 [CONTRIBUTING](CONTRIBUTING.md) §1。

## [0.2.0] - 2026-11-08 — 动态改走关注流（第二位递增：多个重要功能合并）

本版把「Harness 会话交接」「关注流替换逐人轮询」两个重要功能与几处修复合并发布。

登录账号关注名单里的主播后，只读自己的关注流就能拿到全部新动态；逐人轮询退化为可选方式与低频补漏。

### 新增

- **管理台「进程」页**（`/api/tasks`）：主进程以外的后台单元一屏可见 —— 10 个主进程内循环任务
  （直播 / 动态 / 投递 / 周表扫描 / 缓存清理 / QQ 收件 / 清理通知 / 预约刷新 / 日程 AI 识别 / 每日推送）
  可暂停、恢复；弹幕监控台与录播清理守护可启动 / 停止 / 重启；守护进程与主进程只读。开关落
  `meta['tasks_off']`，暂停只停跑、不丢水位、恢复不补推，重启服务后仍然生效。
- **「监控室 ↗」入口**：管理台导航右上角直达多屏直播台 `http://127.0.0.1:8760`；livedeck 在跑时
  「进程」页那一行也有一键「打开」，没跑时给「启动」。
- **动态改走关注流**（`poll.mode` = auto / feed / per_user）：登录账号先关注名单里的所有主播，之后
  只读自己的关注流（`/x/polymer/web-dynamic/v1/feed/all`）就能一次拿回全部新动态——29 个主播从「29 次请求、
  32~34 秒」变成「1 次请求、200 毫秒左右」。翻页以「见到上一轮已知 id」为止（防置顶旧条目误停），首页积压
  （≥15 条）时自动放宽到 3 页并按 `module_author.mid` 过滤名单外条目；两路径共用同一套「已见 + 水位 + 置顶」
  去重口径，所以随时可以在 `config/subjects.json` 里把 `mode` 改回 `per_user` 回滚，不需要迁移数据库。
  缺 `config/bili_follow.env`（SESSDATA + bili_jct）时自动退回逐人轮询并打印原因。
- **关注同步**：启动时和每 6 小时把名单与「自己关注的人」做一次差集，只补关注、绝不取关，间隔 2 秒，
  每次动作写 `logs/follow_sync/follow-sync-<时间>.json` 便于回滚；没有 bili_jct 时跳过并告警。
- **低频补漏**（默认 6 小时一次，`sweep_hours: 0` 关闭）：用逐人接口兜一遍，补上关注流可能吞掉的条目。
  早前 101 关注的对照实验里关注流漏过 23/140 条（16.4%），所以默认保留这道安全网；早于
  `sweep_max_age_min`（默认 30 分钟）的迟到条目只记已见不推送，避免补漏刷屏。补漏在后台任务里跑，不占直播轮。
- **管理台「动态采集方式」卡**（概览页）：显示生效模式、凭据状态、上一轮页数/条目/耗时/字节/risk、
  补漏与关注同步结果，三个按钮「立即取流 / 立即补漏 / 同步关注」直接给采集进程发请求（写
  `data/feed.request` 等文件，下一轮消费），还能一键切 `poll.mode`（需重启生效）。
- 新增 `core/feed_collect.py`（取数策略与关注同步）、`web/console_feed.py`（卡片数据层）、
  `GET /api/feed/status`、`POST /api/feed/action`、`POST /api/feed/mode`、
  `tests/test_feed_collect.py`（16 例）、`tests/test_feed_watcher.py`（7 例）、`docs/35-关注流采集.md`；
  `scripts/run_bot.py` 新增 `--poll-mode`，`scripts/preflight.py` 增加关注流凭据检查。
- 变更：`config/subjects.json.example` 的 `poll` 增加 `mode / feed_interval_s / feed_pages /
  feed_burst_threshold / feed_burst_pages / sweep_hours / sweep_max_age_min / follow_sync /
  follow_sync_hours` 与注释；README 的采集说明、文档索引、代码地图同步更新。

- **日程识别多了第二条路：Harness 会话交接**（`AI_BACKEND=harness`）。不配任何模型 key，改用本机 DSH 会话读图：
  监控台把待判候选写成 `out/ai_tasks/<batch_id>.json`（素材 + 图片绝对路径 + 判读规则），会话按规则读图后把
  `{"results":[...]}` 写进 `out/ai_results/`，监控台回收校验后走原来的 `weekly.ingest` 入库，成功再归档到 `done/`。
  两种用法共用同一套目录：**手动**（管理台「导出任务包」/「导入结果」，或 `weekly_agent.py export|ingest`）与
  **定时**（DSH 自动化每 5 分钟扫一遍，提示词可一键复制）。直连视觉模型的原有行为不变，
  `config/ai.env` 里 `AI_BACKEND` 留空或写 `openai` 即维持原样。
- 新增 `core/weekly/harness.py`（任务/结果文件协议、JSON 容错解析、`event_id` 校验、归档、统计）、
  `tests/test_harness.py`（24 例）；管理台「日程识别模型」卡片增加方式选择、导出任务包、导入结果、复制自动化提示词。

### 变更

- **新增 `AGENTS.md`（项目底层要求）**：把版本迭代规则（第一位人工放行、低位归零、每版必须有文字记录）、
  提交前必跑、凭据与编码硬约束、变更同步清单写成仓库级硬约定，并接入 `release.py check`（关键文件）与
  `tests/test_version.py`（文件里必须写明三位规则与归零示例）。CONTRIBUTING、README、docs/31 §11 均指向它。
  版本规则从此不只散落在文档里，而是「写在底层要求里 + 有测试守着」。

### 修复

- **识别方式切换立刻落盘**：在「日程识别模型」卡片把方式下拉改成 Harness 后，此前只改了界面上的显示，没有写进
  `config/ai.env`，刷新页面又退回「直连视觉模型」。现在下拉一变就 PATCH `{"backend":...}`（只写 `AI_BACKEND`，
  不动 `AI_BASE_URL`/`AI_MODEL`/Key），刷新生效；切回直连但还没填 URL/Model 时不拦，卡片会提示「还缺
  AI_BASE_URL、AI_MODEL」。
- **管理台页面按 mtime 实时读取**：改完 `web/index.html` 刷新浏览器就能看到，不必重启后端（此前页面在 `make_server()` 里只读一次，前端改动必须重启进程才生效）。后端 `*.py` 改动仍需重启：管理台「服务」页的「重启服务」，或写一个空的 `data/restart.request` 交给守护进程优雅重启（不计入崩溃次数）。

## [0.1.0] - 2026-11-08 — 首个可交付版本

> 编号说明：这一版原先记为 `1.0.0`，2026-11-08 起按上面的版本号规则从 `0.1.0` 重新编号；对应 tag 也改为 `v0.1.0`（个人线为 `v0.1.0-personal`）。

首个可交付、可移交他人的版本。核心变化是把「作者本机能跑」变成「任何人拿到包就能跑」。

### 新增

- **一键安装**：`install.cmd` / `python scripts/install.ps1` —— 自动找 Python、建 `.venv`、按固定版本装依赖、
  从 `*.example` 生成配置、建 `data/ logs/ out/` 目录，最后跑一遍环境自检。
- **环境自检** `scripts/preflight.py`：检查 Python 版本、依赖、配置文件、凭据是否占位、数据库可写、端口占用、
  磁盘与版本号，输出人读报告或 `--json`。
- **接入向导** `scripts/setup_wizard.py`：交互式完成「填 QQ 机器人凭据 → 扫码登录 B 站 → 抓取推送目标 openid →
  导入主播名单 → 自检」，每一步都可以跳过，跑完就能启动。
- **版本管理**：`VERSION`（唯一来源）、`shared/version.py`、本文件、`scripts/release.py`
  （`bump` 改版本并写 CHANGELOG 小节 / `check` 扫描是否夹带凭据 / `build` 产出干净 zip + `SHA256SUMS`）。
- **配置模板**：`BBOT.env.example`、`config/bili.env.example`、`config/livedeck.env.example`、
  `config/subjects.json.example`、`config/recorder_cleanup.json.example`、`.p0_ids.json.example`，
  全部用占位值，说明每个字段去哪儿拿。
- **交接文档** `docs/31-发布与交接手册.md`：安装、申请机器人、登录账号、验收清单、日常运维、升级与回滚、备份、卸载。
- **开发历程** `docs/32-开发历程.md`：从 README 迁出的功能演进记录，保留过程信息但不再占满首页。

### 变更

- `requirements.txt` / `requirements-optional.txt` 改为**纯 ASCII**（pip 在中文 Windows 上按系统本地编码读取该文件，含中文注释会直接报 `UnicodeDecodeError`）。
- `requirements.txt` 从宽松下限改为**固定版本**（httpx 0.27.0 / Pillow 13.0.0 / websockets 17.3 /
  websocket-client 1.8.0 / segno 1.6.6），可选依赖拆到 `requirements-optional.txt`。
- 启动脚本全部改为**自定位**：`scripts/service_start.cmd`、`scripts/service_hidden.vbs`、
  `scripts/install_cleanup_autostart.ps1`、`scripts/service.ps1`、`start_livedeck.bat` 不再硬编码
  `<本机项目路径>` 与本机 Anaconda 路径，优先使用项目内 `.venv\Scripts\python.exe`。
- `README.md` 重写为「这是什么 / 五分钟上手 / 常用命令 / 目录结构 / 文档索引 / 版本与发布 / 安全基线」，
  原来那整段功能演进史移到 `docs/32-开发历程.md`。
- `docs/03-部署教学手册.md` 顶部标注为历史草稿（Docker/NAS 路线），Windows 原生部署以 31 号文档为准。

### 安全

- `.p0_ids.json`（含真实推送 openid）移出版本库，改为 `.p0_ids.json.example` + `.gitignore` 忽略。
- 发布包构建时做凭据扫描：`SESSDATA`/`bili_jct`/`QQBOT_APP_SECRET` 真值、32 位 hex openid、
  `data/` `logs/` 与任何 `.env` 文件都不会进包。
- 安装脚本生成配置后调用 `scripts/secure_config.py` 用 `icacls` 收紧 ACL（仅当前用户可读），
  `install.ps1 -SkipAcl` 可跳过。

### 修复

- `install.ps1` 在 **Windows PowerShell 5.1** 下找不到 Python：5.1 会把传给原生程序的
  `-c "..."` 里的双引号剥掉，导致探针脚本语法错误。改用不含引号的探针
  `import sys;print(*sys.version_info[:3])` 后两个版本都正常。
- 含中文的 `.ps1` 统一保存为 **UTF-8 with BOM**：PowerShell 5.1 按 ANSI 读取无 BOM 的文件时，
  中日韩字符的尾字节可能吞掉紧随其后的 `{` `}`，直接报「Missing closing '}'」。
  `release.py check` 新增该编码检查。
- pip 安装参数加 `--timeout 60 --retries 3 --prefer-binary`，弱网下不再一超时就整体失败。
- `preflight.py` 对「全新安装还没接入账号」的状态不再报阻塞：`BBOT.env` / `.p0_ids.json` /
  `config/subjects.json` 缺失时降为警告，并直接提示下一步 `python scripts/setup_wizard.py`；
  只有「文件存在但内容是占位值 / JSON 写坏 / 依赖缺失」才算阻塞。
- `release.py` 打包时过滤 `tmp*`、`pip-*` 这类本机临时目录（曾导致 build 报 `PermissionError`）。

### 升级提示

- 从作者本机的历史版本迁移时：把旧的 `config/subjects.json`、`BBOT.env`、`config/bili.env`
  放回同名位置即可；数据库 `data/bot.db` 会在首次启动时按 schema 顺序迁移（当前 schema v4）。

### 新增（2026-11-08 追加）

- **机器人页（管理台）**：QQ 机器人绑定从命令行搬进监控台。填 AppID / AppSecret → 立即校验并显示机器人名 →
  点「开始监听」后按提示私聊机器人发一句话，页面自动抓到 `user_openid`（群聊同理拿 `group_openid`）→
  可直接发测试消息。也支持手动粘贴兜底与清空。公共实现在 `channels/qq_setup.py`，命令行
  `scripts/p0_check.py` 改为它的薄壳，两条入口不会各写一套 HTTP 细节。见 `docs/12` §23。
- **产品内 Agent 日程识别**：不再必须让外部会话来读图。
  - `shared/ai.py`：OpenAI 兼容客户端，支持图片（data URL，长边先缩）、从带寒暄的回复里抠 JSON、
    把 401/404/429/400/5xx 翻译成「下一步改什么」。
  - `core/weekly/agent.py`：系统提示词与 `week / single / none` 三种口径、强制二次判读
    （`confirmed / downgraded / unsure`）、素材组装（正文 + 周表图 + 预约卡 + OCR + 同主播近期场次）、
    结果洗字段后走既有的 `weekly.ingest` 入库，复核与人工优先规则完全不变。
  - `core/weekly/batch.py`：候选批次组装从 `scripts/weekly_ai.py` 抽出，新旧两条路径共用。
  - 管理台日程表页新增「日程识别模型」卡：配置 base_url / model / key（掩码回填）、测试连接、立即识别、进度与最近结果。
  - 常驻循环 `weekly_agent_loop`（进程页可开关）：到时段且有待确认候选、且已配置模型时才跑。
  - 配置模板 `config/ai.env.example`（OpenAI / 阿里百炼 / 智谱 / 本机 Ollama 四组示例）。
  - 文档 `docs/33-AI日程识别.md`：选型与成本、Ollama 装法、配置三方式、判读与复核口径、失败排查、隐私、怎么关掉。
- **测试**：`tests/test_qq_setup.py`（13）、`tests/test_ai.py`（22）、`tests/test_weekly_agent.py`（14），
  全部离线 mock，单元测试总数 87 → 136。

### 开源发布

- 仓库以 **MIT** 许可公开（开发线仍保留「个人自用 / 非商业」条款，两条线的关系见 `docs/34-开源发布清单.md` §9）。
- 新增协作文件：`CONTRIBUTING.md`（环境、测试、代码约定、提交信息、PR 与自查清单）、
  `CODE_OF_CONDUCT.md`、`SECURITY.md`（支持版本、漏洞报告渠道、本项目持有哪些敏感数据）。
- 新增 `.github/`：`workflows/ci.yml`（Windows + Python 3.10/3.11/3.12 跑单元测试、
  `release.py check`、全包 `compileall`；另有两个 Linux 作业做内联 JS 语法检查与文档内链检查）、
  `ISSUE_TEMPLATE/bug_report.yml`、`feature_request.yml`、`config.yml`、`pull_request_template.md`。
- 新增 `.gitattributes`：文档与代码 LF、`.cmd/.bat/.ps1/.vbs` CRLF、图片二进制。
- `README.md` 加 CI / License / Python / Platform 徽章与「参与贡献」小节。
- `docs/34-开源发布清单.md`：建仓库、替换占位符、仓库设置、首个 Release、发版流程、两条分支如何同步。
- 文档配图纳入 `docs/img/`（`.gitignore` 的 `img/` 改为只忽略仓库根 `/img/`）。

### 变更（2026-11-08 追加）

- 命令行的 `scripts/p0_check.py` 现在支持 `--timeout 0` 无限等待，并区分「本次没收到新消息」与「未绑定」。
- `scripts/weekly_ai.py` 瘦身为「闸门 + 导出批次 JSON」的薄壳，素材组装逻辑移到 `core/weekly/batch.py`；
  `gate()` 等模块级函数保持兼容（`scripts/selftest_weekly.py` 仍直接调用）。
- `release.py check` 的关键文件清单加入 `config/ai.env.example`、`shared/ai.py`、`core/weekly/agent.py`、`docs/33-AI日程识别.md`。
