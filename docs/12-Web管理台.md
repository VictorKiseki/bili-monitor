# 12 · Web 管理台（T5，已实现并实测）

> 日期：2026-10-28　状态：已完成
> 命名：本页是「DD全能工作间」的 Web 管理台「监控台」，另一半是多屏直播台「监控室」（[docs/23](23-多屏直播监控台.md)）；页脚版权为 © 2026 VictorKiseki。
> 上游：docs/02 `9（Web 台设计）、D11（只监听 127.0.0.1）、docs/11（主循环与存储）
> 三个代码文件：[web/console.py](../web/console.py)、[web/index.html](../web/index.html)、[scripts/run_console.py](../scripts/run_console.py)

---

## 0. 一句话结论

管理台已经能看能改：**名单增删改、直播状态、事件流水、投递与死信重投、日志按日期/类型/级别筛选、录播清理（开关/设置/状态/日志/登录自启），外加顶栏三段全局总开关（全开/只采集/全停）**，全部走同一个 `data/bot.db`。
只监听 127.0.0.1，非环回来源一律 403；零新依赖（只用 Python 标准库 `http.server`，前端是单文件原生 JS）。

---

## 1. 两种启动方式

```bash
# A. 单独一个进程（主循环已经在跑的时候用这个）
python scripts/run_console.py                      # http://127.0.0.1:8765
python scripts/run_console.py --port 9000 --open   # 换端口 + 自动开浏览器

# B. 和主循环同一个进程（最省事）
python scripts/run_bot.py --channel qq --web       # 采集 + 推送 + 管理台一起
```

两种方式读写同一个 SQLite（WAL），**同时开也不冲突**：主循环写，管理台读，偶发写入（改名单/重投）走短事务。
日志页按**目录**读按日日志：`logs/` 下的 `collector-YYYY-MM-DD.log`（采集）与 `pusher-YYYY-MM-DD.log`（推送），
三进程拆分前的 `bot-YYYY-MM-DD.log` 也认。监控台主进程自己的 stdout 落在 `out/console.log`，不属于这个目录；
用 `run_console.py --log-dir <目录>` 可以改读哪里（2026-11-14 之前这里从 `--log-file` 的父目录推，指到了 `out/`，页面一直是空的）。

当前界面见[监控台概览](img/readme-console-overview.png)和[监控名单](img/readme-console-subjects.png)。
原联调的事件、投递、总开关和日志筛选截图保存在开发机的 `out/` 目录。

---

## 2. 页面与接口

| 页面 | 看什么 | 对应接口 |
|---|---|---|
| 概览 | 主播数 / 在播数 / 事件数 / 待发 / 失败 / 库大小；推送目标开关（一个都没绑定时，空态给按钮直达「机器人」页，见 §22.12） | `GET /api/overview`、`PATCH /api/targets/{id}` |
| 监控名单 | 28 个号的房间号、直播状态（直播中/轮播按未开播）、基线是否建立、最后轮询时间 | `GET /api/subjects` |
| 事件 | 最近 80 条事件：时间、主播、类型、标题（可点开）、投递结果；可按主播过滤；标题行显示**表内总数**（不是本页条数），超出本页时追加「· 本页显示最近 80 条」 | `GET /api/events?limit=&uid=`（返回 `{events[], total}`） |
| 投递 | 按状态过滤（待发/失败/已发/已忽略），失败可「重投」或「忽略」（忽略 = 确认不再发，随事件过期一起清掉） | `GET /api/deliveries`、`POST /api/deliveries/{id}/retry`、`POST /api/deliveries/{id}/dismiss` |
| 日志 | 按日期 / 类型 / 级别 / 关键字筛选（第 8 节），可勾选 3 秒自动刷新 | `GET /api/logs?lines=&date=&tag=&level=&q=` |
| 录播清理 | 守护状态、自动清理开关、上限/保底/保护时间/检查间隔（天）+执行时间、待确认计划、立即扫描/检查并通知、启动/停止/重启、登录自启、清理日志（第 14 节） | `/api/cleanup/*` |
| 顶栏开关 | 全开 / 只采集 / 全停；暂停时整条横幅提示，并给出「丢弃积压待发」。右侧「已发 / 待发 / 失败」三个徽标可点，直接跳到对应状态筛选的投递列表 | `GET /api/global`、`PATCH /api/global`、`POST /api/deliveries/flush` |

写操作两个：

| 操作 | 接口 | 语义 |
|---|---|---|
| 加号（UID 或名字） | `POST /api/subjects {uid}` 或 `{keyword}` | 名字走 B 站搜索（WBI，免登录）列出候选，点「加这个」才写入 |
| 移除 | `DELETE /api/subjects/{uid}` | 删名单与水位，**保留**历史事件与已见动态 |

搜索结果接口单独一个：`GET /api/search?q=`。

**写操作会留一行日志**（2026-11-14）：所有 `POST` / `PATCH` / `DELETE` 成功之后，
`web/console_http.py` 的 `_audit()` 往 `out/console.log` 写一行

```text
2026-11-14 13:02:11 INFO [写操作] POST /api/targets/3 → 200（12 ms）
```

只记方法、路径、状态码、耗时，**不记请求体**——body 里有 SESSDATA、openid、代理密码，
落日志会违反 AGENTS §4。失败（≥400）与 GET 不记，避免把日志刷满。
这条是为了回答「刚才谁把推送目标停了 / 谁点了全停」——以前这些操作没有任何记录。

---

## 3. 改了名单，主循环会怎样

- **加号**：同时写 `config/subjects.json` 与 `subject` 表。主循环每轮都重新 `list_subjects()`，
  所以不用重启；新号没有水位记录 → 下一轮动态轮询走「基线分支」，**只记历史不推送**（docs/05 第 9.1 条）。
- **移除**：从 config 和 `subject`/`subject_state`/`live_session` 删除，`event` 与 `seen_dynamic` 保留
  → 以后再加回来也不会把旧动态补推一遍。
- **停用（已取消）**：2026-11-10 起名单不再提供启用 / 停用，所有主播一律轮询；历史被停用的号由 schema v5 迁移抬回 `enabled=1`（水位、推送开关、直播会话都保留）。「名单外自动对账」仍会在启动时停用不在 `config/subjects.json` 里的库行；镜像回写只写启用行（`WHERE enabled=1`），停用行不会被带回名单，见 docs/35 §6.1。

---

## 4. 安全边界（为什么不做登录）

| 措施 | 实现 |
|---|---|
| 只绑定环回 | 默认 `--host 127.0.0.1`，文档不提供对外暴露的用法 |
| 双保险：来源校验 | 除绑定外，处理器再检查 `client_address` 与 `Host` 头，非 `127.0.0.1/::1/localhost` → 403（实测 `Host: evil.com` 返回 403） |
| 无写关键操作 | 不能改通道凭据、不能改轮询间隔、不能发任意消息，只能改名单/开关/重投/忽略 |
| 端口 | 8765，仅本机，Windows 防火墙默认不放行 |

要跨机器用请走 SSH 隧道（`ssh -L 8765:127.0.0.1:8765`），而不是把 `--host` 改成 `0.0.0.0`。

---

## 6. 顺带修掉一个真 bug：下播事件时间变成 1970

联调时发现 `event` 表里有一条 `occurred_at = 0` 的下播事件（截图上「时间」列显示「—」）。

原因：`collector/events.py` 的 `event_from_live()` 用 `occurred_at = start_ts or 0`，
而下播时直播接口返回的 `live_time` 已经是 0（人已经下播了，没有开播时间可给）。

修复：`event_from_live(prev, cur, now=None)` → `occurred_at = start_ts or int(now if now is not None else time.time())`，
Watcher 调用时传 `now=self._now()`；只有下播会落到「检测到的那一刻」。
已加回归断言 `下播 occurred_at 不为 0`（`scripts/selftest_watcher.py`），28 条用例仍全 PASS。
库里那条历史脏数据已就地修正为 `created_at`。

---

### 在线更新

监控台和监控室用 GitHub 最新正式发行版标签与本地 VERSION 比对。已安装更新公钥、由 Windows 守护进程管理的发行版可从 GitHub 下载安装增量附件。
`GET /api/update/status` 返回本机状态。`POST /api/update/apply` 接收 `{"target":"版本号"}`，遵循同源和 CSRF 校验。
守护下载、校验文件后才停止进程。新版管理台通过本机启动检查后恢复后台进程，失败则回滚代码。
关闭独立直播监控室后可更新。若当前版本没有对应增量包，使用完整发行包升级。

## 7. 全局总开关（2026-10-28 补）

页面顶栏右上角三段开关，存在数据库 meta 表里（key = global_mode），主循环**每轮重新读**，所以改完最多一个轮次生效，不用重启。

| 档位 | 值 | 采集 | 推送 | 典型用途 |
|---|---|---|---|---|
| 全开 | full（默认） | 是 | 是 | 正常工作 |
| 只采集 | collect | 是 | 否 | 人不在电脑边，先攒着不发；事件照常入库可事后查 |
| 全停 | off | 否 | 否 | 维护/换配置；不采集不发消息，但心跳照写，守护不会把它当卡死重启 |

行为细节：

- **off**：tick 直接返回，不调 B 站接口；心跳写 `last_tick`，所以守护进程的卡死检测不会被误触发。
- **collect**：照常轮询、照常写事件表，但 `publish()` 只 `insert_event` 不入队 —— 不产生 delivery 行，也就不会在恢复后有历史积压补发。
- **恢复 full**：继续派发暂停前遗留的待发投递。collect 期间的事件只入库，留在「事件」页供查看。
- **更换 QQ 收件人**：旧收件人的待发与未完成投递标为「已忽略」，发送历史保留。新投递使用新目标。
- **丢弃积压待发**：非 full 时横幅右侧会出现按钮，把 `status='pending'` 的投递一次性标成 failed（附原因「手动丢弃（积压待发）」）。用在「full 期间积压了一堆没发出去、恢复后不想被刷屏」的场景。

接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/global` | 当前档位、三档说明、待发条数 |
| PATCH | `/api/global` | body `{"mode":"full"|"collect"|"off"}`，非法值 400 |
| POST | `/api/deliveries/flush` | 丢弃全部待发，返回丢弃条数 |

自测：`python scripts/selftest_switch.py`（12 条断言，覆盖 off 不打接口/心跳照写、collect 入库不投递、恢复 full 只发新事件、非法值不炸）。

### 7.1 长跑抓到的一个真 bug：写库不 commit 会把主循环锁死

2026-10-28 02:47 长跑中第一次切总开关，主循环当场崩了：

```
store/sqlite.py 的 _save_state → sqlite3.OperationalError: database is locked
守护：子进程异常退出（返回码 1），第 1 次重启 → 5 秒后重启 → 又崩 → 第 2 次重启
```

根因不在主循环，在管理台：`Console._x()` 执行完 INSERT **没有 commit**。
python 的 sqlite3 默认不自动提交，于是管理台进程一直挂着一个未提交写事务；
主循环那边再写同一张库，等满 `busy_timeout=5000` 就抛锁错误。

修了三处（都是「防御性」而不是「绕过」）：

| 位置 | 改法 |
|---|---|
| `web/console.py` `_x()` | 执行后立刻 `conn.commit()`，绝不留悬空事务 |
| `store/sqlite.py` `_run()` | 捕获含 locked/busy 的 `OperationalError`，按 0.2s / 0.5s / 1.5s 重试 3 次再抛 |
| `core/watcher.py` | `run()` 单轮异常只记日志 + 写心跳 `tick_error`；`round_live` 每个号单独 try，坏一个号不带走整轮 |

回归自测：`python scripts/selftest_console_lock.py`（临时库验证「管理台写完后别的连接能立刻写」）。
修完用脚本连切 36 次总开关（off/collect/full 各 12 次），子进程 0 重启、日志 0 条 `[错误]`。

---

## 8. 日志筛选（按日期 / 类型 / 严重程度，2026-10-28 补）

改之前日志页只能 tail 一个文件，翻历史得自己开编辑器。现在四个维度都能筛：

| 维度 | 选项 | 说明 |
|---|---|---|
| 日期 | 最新一天 / 全部日期 / 具体某天 | 只列按日结构化日志；同一天的采集 + 推送合并成一项（下拉显示「2026-11-14 · 采集+推送 · 23KB」） |
| 类型 | 全部类型（带条数）/ 具体标签 | 标签就是日志里的 `[启动]``[动态]``[通道]` 这些 |
| 严重程度 | 错误 / 警告 / 信息（带条数） | 由标签 + 关键字判定，见下表 |
| 关键字 | 任意串 | 对正文做子串匹配，回车即查 |

严重程度不是靠人肉标注，是 `core/logfmt.py` 的 `classify()` 判的：

| 级别 | 判定规则 | 例子 |
|---|---|---|
| 错误 | 标签是 `[错误]`，或正文含 失败/异常/错误/崩溃/Traceback/Error/locked/拒绝/超时/不可用 | `白き射手ナナリー 取数失败：… code=-352` |
| 警告 | 标签是 `[总开关]``[停止]``[死信]`，或正文含 为空/重试/暂停/停用/丢弃/退避/重启/缺少/未开播/轮播 | `残Tony 本轮为空（重试 2 次），跳过且不动水位` |
| 信息 | 其余全部 | `[通道] 官方 QQ，AppID 1028****99，token 正常：True` |

**零值计数不算错误**：`错误 0`、`失败 0`、`异常 0` 这类汇总在判级前会先被剔除，`[直播预约] 新增/更新 2 条，跳过 1，错误 0` 现在是信息级；只有真实计数 `错误 N`（N>0）才是错误。预约汇总也不再无条件打印「跳过 0 / 错误 0」。

**轮次统一命名**：主循环现在写 `[主循环轮次] 第 419 轮 …`；`parse_line()` 也会把历史日志里的 `[轮次 419]` 归一成同一个类型 `主循环轮次`，所以类型筛选和统计不会再按轮次数字拆成一堆标签。

表格是四列：时间 / 级别徽章 / 类型 / 内容。错误行整行底色偏暗红，扫一眼就能定位。

**日志按天落盘**：`--log-file logs\bot.log` 自动写成 `logs\bot-YYYY-MM-DD.log`，跨天自动换文件
（`core/logfmt.py` 与 `scripts/run_bot.py` 的 `DailySink`），行首时间戳也带了日期。文件名前缀就是
`--log-file` 的 stem：新链路传 `logs/collector.log` 就落成 `collector-YYYY-MM-DD.log`，推送进程同理。
老的 `logs\bot.log`（没有日期前缀、行首只有时分秒）仍会被列出来，标成「历史 bot.log（旧格式）」，
旧行没有日期，按日期筛时只会出现在「全部日期」里。

**拆分后同一天有两份日志**（2026-11-14 修）：三进程改造把主循环拆成采集（`run_bot.py --collector`，写
`logs/collector-YYYY-MM-DD.log`）与推送（`run_pusher.py`，写 `logs/pusher-YYYY-MM-DD.log`）两个进程，
所以「最新一天」现在会**同时读这两份**，合并后按行首时间重排再切 `lines` —— 只读一份会漏掉另一半。
`list_logs()` 认 `bot` / `collector` / `pusher` 三个家族，每条文件记录带 `kind`（家族）与 `label`；
返回的每行也带 `src`（这行来自哪个家族）。`collector.console.log` / `pusher.console.log` 这类
控制台捕获文件不是按日结构化日志，不列出来。

接口参数（都在 `GET /api/logs`）：

| 参数 | 缺省 | 说明 |
|---|---|---|
| `lines` | 200 | 1~2000，超出被夹住 |
| `date` | 最新一天 | `all`=全部日期，或 `YYYY-MM-DD`；同一天有多份日志（采集 + 推送）时一起读 |
| `tag` | 空=全部 | 标签精确匹配 |
| `level` | 空=全部 | 只接受 `error`/`warn`/`info`，别的值 400 |
| `q` | 空 | 正文子串，不区分大小写 |

返回里除 `rows` 还有 `picked`（本次扫了哪几个文件）、`scanned`（扫了多少行）、
`counts`（三档各多少条）、`tags`（每个标签多少条），页面上的「错误 N / 警告 N」就是这么来的。

只从文件尾读最近 2MB，单个文件很大时不会卡住；统计数字按**本次扫描窗口**算，不是全库统计。

选「全部日期」时有扫描上限：最多读 **90 份日文件、累计 32MB**（`core/logfmt.py` 的
`MAX_SCAN_FILES` / `MAX_SCAN_BYTES`），撞到上限时返回 `limited=true` 与 `scanned_bytes`，
页面在统计行后面追加「已到扫描上限（只扫最近 N 份 · X MB，想更早请选具体日期）」。
这个上限只影响「全部日期」，选具体某天或「最新一天」仍是完整读取。

自测：`python scripts/selftest_logs.py`（34 条断言：解析、旧格式兼容、级别判定、零值计数、轮次归一、文件列表、四维筛选、limit、统计、拆分后采集/推送家族合并）。

---

## 9. 已知限制

| 限制 | 说明 |
|---|---|
| 没有登录/令牌 | 依赖「只在本机」这一条，多用户机器上不安全 |
| 页面 3 秒轮询 | 不是 WebSocket，数据量小没问题；清理页状态和日志也跟着这个节奏刷 |
| 并发写 config | 管理台和手工编辑 `subjects.json` 同时写会互相覆盖（单用户场景可接受） |
| 日志统计只覆盖扫描窗口 | 只读文件尾 2MB，且统计按窗口算；旧 `bot.log` 没有日期，只在「全部日期」里出现 |
| 「全部日期」有扫描上限 | 最多 90 份 / 32MB，超出返回 `limited` 并在页面上说明；历史更早的日志请选具体日期 |
| 日志页自动刷新默认关 | 想实时看就勾「3 秒自动刷新」；不勾是为了不打断你翻历史 |
| 无批量导入 | 一次一个号；大批量仍用 `scripts/import_by_names.py` |
| collect 期事件不自动补发 | 只采集时入库的事件，恢复全开后不会补发（避免刷屏），需要时只能逐条重投 |

---

## 10. 下一步

1. ~~**守护**~~ → 已完成，见 `docs/13-守护与开机自启.md`。
2. **真通道长跑 120 分钟**：观察死信率、token 续期、下播事件时间是否正确（2026-10-28 已启动，见 `docs/14-长跑观察记录.md`）。
3. 可选小功能：管理台加「发一条测试消息」按钮（复用已有 Dispatcher，不必新写通道逻辑）。

---

## 11. B 站登录态面板（2026-10-28 补）

概览页「B 站登录态（SESSDATA）」：**看得见状态，也填得进去**。

| 操作 | 接口 | 行为 |
|---|---|---|
| 看状态 | `GET /api/bili/auth` | 返回 `configured / valid / uname / mid / masked / message`；结果缓存 60 秒，`?force=1` 强制复查 |
| 填写 | `POST /api/bili/auth {sessdata}` | **先用这条值请求 nav 校验，通过才写盘**；原子替换 `config/bili.env` 里的 `SESSDATA` 行，其它注释原样保留，权限 0600 |
| 清除 | `DELETE /api/bili/auth` | 把该行改回注释，重启后回到匿名取数 |
| 生效 | `POST /api/service/restart` | 写 `data/restart.request`，守护让子进程优雅退出后立即重启（不计崩溃次数），见 13 §9 |

> 命令行等价物：`pwsh -File scripts\restart_console.ps1`（只重启管理台主进程，守护不动；
> `-Status` 只看端口 / 守护 / 进程链 / HTTP），见 [13 §9.4](13-守护与开机自启.md)。

前端：状态徽章（未配置 / 已启用 / 无效）+ 脱敏值（如 `9eee****IIEC（222 位）`）+ 账号名与 mid + 一句话原因；密码框回车即提交，按钮带「校验中…」态。

### 为什么加这个

之前 `run_bot.py` 启动时打印「已启用（**有效**）」是假的——只判断了配置文件里有没有值（`client.logged_in`），没真去校验。2026-10-28 07:35 实测发现：用户的 SESSDATA **已经过期**（nav 返回 `code=-101 账号未登录`），日志却一直说有效，等于把「取数变差」的原因藏起来了。

现在启动时会真调一次 nav：

- 有效 → `[B站] 动态取数登录态：已启用（有效，账号 @xxx mid=yyy）`
- 无效 → `[B站] 动态取数登录态：已填 SESSDATA 但无效（账号未登录）→ 按匿名模式跑，请到管理台「B 站登录态」重新粘贴`
- 没填 → 匿名模式提示

## 12. 扫码登录面板（2026-10-28 补）

§11 解决的是"看得见 + 填得进去"，但**凭据从浏览器抠出来本身就是根因**：浏览器退出/换号，那次会话被吊销，SESSDATA 立刻作废，而日志只会静默变差。§12 让采集进程**自己扫一次码**，拿一份和浏览器无关的设备会话（完整原理与边界见 [18 号文档](18-登录态独立方案.md)）。

| 操作 | 接口 | 行为 |
|---|---|---|
| 出码 | `POST /api/bili/qr/new` | 调 B 站 `qrcode/generate`，返回 `qrcode_key` + `svg` + `expires_in=180` |
| 轮询 | `GET /api/bili/qr/poll?key=…` | `status`：`waiting` / `scanned` / `expired` / `success` / `error`；**只有 success 才写盘**，并返回 `need_restart=true` |
| 生效 | `POST /api/service/restart` | 同 §11，扫码成功后由你决定何时重启 |

前端：登录态卡片里一个「扫码登录（推荐，不依赖浏览器）」按钮 → 二维码（白底 SVG）+ 状态行 + 「取消扫码」；每 2 秒轮询一次，`success` 后自动刷新卡片状态（但不自动重启，避免打断正在跑的轮次）。

### 为什么不再只靠粘贴

| 方式 | 凭据归属 | 浏览器退出/换号 | 需要 F12 |
|---|---|---|---|
| 粘贴 SESSDATA | 浏览器那次登录会话 | **失效** | 需要 |
| 扫码 | 采集进程自己的设备会话 | **不受影响** | 不需要 |

粘贴入口保留作兜底（两处写的是同一个 `config/bili.env`、同一种 `SESSDATA=` 行）。

## 13. 右上角计数实时刷新（2026-11-01 补）

### 现象

顶部右侧那排 pill（主播 / 直播中 / 已发 / 待发 / 失败）只在点回「概览」时才变。切到「监控名单 / 事件 / 投递 / 日志」之后，哪怕新事件已经发出去、待发数已经归零，右上角还是老数字。

### 根因

两件事叠在一起：

1. pills 的 HTML 是在 `loadOverview()` 里拼的（跟着 `/api/overview` 一起），页面没有独立的计数接口；
2. 定时器按页签分档：切换页签时 `setInterval(refresh, name === 'logs' ? 3000 : 5000)`，而 `refresh()` 里只有 `state.tab === 'overview'` 才调 `loadOverview()`。

→ 页面本身在跳，只是没人给 pill 喂数。而且 `overview()` 还要带 targets、数据库大小这些重家伙，不适合 3 秒一次。

### 改法

**服务端**：新增轻量接口，只做 4 条 COUNT。

```python
def stats(self) -> dict:                 # web/console.py
    subs   = self._q("SELECT COUNT(*) c, SUM(enabled) e FROM subject")[0]
    living = self._q("SELECT COUNT(*) c FROM subject_state WHERE live_status=1")[0]["c"]
    ev     = self._q("SELECT COUNT(*) c FROM event")[0]
    dl     = {r["status"]: r["c"] for r in self._q(
        "SELECT status, COUNT(*) c FROM delivery GROUP BY status")}
```

路由加在 `/api/overview` 旁边：

```python
if method == "GET" and path == "/api/stats":
    return self._send(c.stats())
```

使用虚构数据的响应示例：

```json
{"ok": true, "now": 1800000000, "subjects": {"total": 6, "enabled": 6, "living": 4},
 "events": {"total": 42}, "deliveries": {"pending": 0, "sent": 42, "failed": 0}}
```

**前端**：把 pill 抽成函数，和页签解耦。

```javascript
function pill(label, val, cls, key) {          // key 用来判断『这个数字变了』
  state.pillPrev = state.pillPrev || {};
  var prev = state.pillPrev[key];
  var upd = (prev !== undefined && prev !== val) ? ' upd' : '';   // 变了才闪一下
  state.pillPrev[key] = val;
  return '<span class="pill ' + (cls || '') + upd + '">' + label + ' <b>' + val + '</b></span>';
}
function loadStats() {
  return api('/api/stats').then(function (o) {
    renderPills(o.subjects, o.deliveries);
  }).catch(function () { /* 计数失败不打断页面，下一个 3 秒再试 */ });
}
```

`refresh()` 里无条件调一次，和当前在哪个页签无关：

```javascript
function refresh() {
  loadGlobal();
  loadStats();            /* 顶部计数与页签无关，每次都刷 */
  if (state.tab === 'overview') loadOverview();
  ...
}
```

定时器统一成 3 秒：`state.timer = setInterval(refresh, 3000);`

数字变化时给 pill 加 `.upd`，走一段 0.9 秒的背景/边框回落动画，扫一眼就知道『刚才跳了』：

```css
@keyframes pillupd{0%{background:#243447;border-color:#2e5d78}100%{background:var(--panel2);border-color:var(--line)}}
.pill.upd{animation:pillupd .9s ease-out}
```

### 一个必须记住的坑

`make_server()` 启动时 `Handler.index_html = index.read_text("utf-8")` —— HTML 是**读进内存**的，改完 `web/index.html` 必须重启服务（`pwsh -File scripts\service.ps1 restart`），否则浏览器刷新拿到的还是旧页面。

### 一点取舍

3 秒轮询对 5 个页签都在跑，比原来「只在概览页 5 秒一次」略费，但 `/api/stats` 是纯 COUNT（WAL 只读，微秒级），管理台又只监听 127.0.0.1，这点开销换「数字永远是对的」很划算。真要更快，把 `3000` 改成 `1000` 即可，接口扛得住。

## 14. 录播清理页（2026-11-02 补，同日改版）

把独立的 `scripts/recorder_cleanup.py` 接进管理台：不用再命令行改配置、看日志、猜守护在不在跑。清理器本身仍然是独立进程（不塞进采集主循环），管理台只负责配置、状态、待确认计划展示和进程生命周期。

2026-11-02 按用户要求改成两阶段：**按天+时间检查 → Bot 通知 → 1 小时确认 → 保护/删除**。清理器不再扫描到就直接删。

### 页面

| 区域 | 内容 |
|---|---|
| 状态卡 | 守护运行/PID/运行时长、自动清理开关、下次检查时间、待确认数量/截止、录像上限/磁盘保底、磁盘剩余、上次清理删除/保护数与释放量、保护目录 |
| 自动清理 | 启用/停用；停用写 `enabled=false` 并让守护退出，登录自启快捷方式保留 |
| 操作 | 立即扫描（强制 dry-run）、立即检查并通知（不再直接删）、启动/停止/重启守护 |
| 设置 | `max_gb`、`min_free_gb`、`keep_recent_minutes`、`interval_days`、`run_time`、`dry_run`；`root` 与保护目录只读展示 |
| 待确认清理 | 当前计划 id/原因/候选总数/总大小、每个候选的编号/房间/文件/大小/录制时间/状态、截止时间、已标记保留编号 |
| 登录自启 | 加入/移除当前用户的 Startup 快捷方式（调用 `scripts/install_cleanup_autostart.ps1`） |
| 日志 | 读 `logs/recorder_cleanup.log`，按级别/关键字筛选，3 秒自动刷新；只在自己滚动到底部时自动跟到底 |

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/cleanup/status` | 运行状态、PID、锁信息、phase、last_run、last_review、review、schedule、last_error、配置、磁盘、日志元信息、自启状态 |
| GET | `/api/cleanup/config` | 当前公开配置（含 `interval_days`/`run_time`/`review_wait_minutes`/`protected_dir`） |
| PATCH | `/api/cleanup/config` | 可设置容量、检查时段和 dry-run。仅在首次配置时接受 `root`，必须是已存在目录的完整路径 |
| POST | `/api/cleanup/enable` | body `{enabled}`；启用会写配置并拉起守护，停用会写配置并让守护退出 |
| POST | `/api/cleanup/start` / `stop` / `restart` | 进程生命周期；停止用 `logs/recorder_cleanup.stop`，不是强杀 |
| POST | `/api/cleanup/scan` | 强制 dry-run，只读，返回候选/会删数量 |
| POST | `/api/cleanup/run` | 立即检查：dry-run 只预览；否则确保守护在跑并写 `logs/recorder_cleanup_run.request`，返回触发结果，不直接删 |
| POST | `/api/cleanup/autostart` | body `{enabled}`；加入/移除 Startup 快捷方式 |
| GET | `/api/cleanup/logs?lines=&level=&q=` | 解析清理日志，返回 rows/scanned/counts |

### 设计要点

- **进程控制不靠 PID 强杀**：`start` 用 `pythonw.exe` 脱离父进程启动 `--loop`；`stop` 写 stop 文件，清理循环每秒检查后自行退出；`status` 的 PID 只用于显示和存活判断。
- **调度持久化**：`interval_days` + `run_time` 的指纹和 `next_run_at` 写在 `logs/recorder_cleanup_schedule.json`；重启后沿用，改配置会重置，停机错过的周期不会补跑多次。
- **计划与决定分文件**：守护写 `logs/recorder_cleanup_review.json`；Bot 只写 `notify.json` / `decisions.json`，避免两个进程抢写同一文件。用户决定按 `plan_id` 隔离，旧计划的回复不会误伤新计划。
- **通知没发出去就绝不删**：只有 Bot 写 `notify.json: sent` 后才写 `deadline_at`；发送失败按 60s/300s 重试，失败期间计划保持 `awaiting_notify`。
- **保护移动优先于删除**：到点先 `_move_kept`，按房间子目录移到 `root/_protected`，重名加时间戳；移动失败的文件留 `move_error`，计划保持 `executing`，不会进入删除分支。
- **配置仍是唯一真源**：`config/recorder_cleanup.json`；管理台只是读写它，守护每轮重读。写盘用临时文件 + `os.replace`，避免半截 JSON。
- **状态文件**：`logs/recorder_cleanup_status.json`，记录 phase/started_at/last_run/last_review/review/last_error；状态文件坏了就回退为空，不阻塞页面。
- **安全边界**：清理接口和原有接口一样经过环回 + Host 双校验；清理页没有公网暴露。
- **3 秒刷新不覆盖未保存表单**：页面维护 `cleanupState.dirty`，用户改过设置后，状态轮询只更新状态卡，不回填表单。

## 15. 自定义分组与批量处理（2026-11-06 补）

监控名单从「平铺一列」升级成「可分组 + 可批量」。27 个主播还能靠眼睛找，再多就必须分组；一次改十几个号的分组也不该点十几次。分组可以先建空的再接人：新建不要求先勾号，空分组也会一直留在下拉里，直到你删掉它。

### 数据放在哪

分组落在 SQLite 的 `subject.grp` 列（`TEXT NOT NULL DEFAULT ''`，空串 = 未分组），**不是** config 里的数组字段：

- 运行时真实来源是 `store.list_subjects()`（[core/watcher.py](../core/watcher.py) 每轮读库），config 只是管理台回写的镜像；
- 分组若只写 config，下次回写就会被库里的旧值覆盖。

老库不用手动迁移：[store/sqlite.py](../store/sqlite.py) 的 `migrate()` 会 `PRAGMA table_info(subject)` 检查，缺 `grp` 就 `ALTER TABLE subject ADD COLUMN grp TEXT NOT NULL DEFAULT ''`，历史号自动归到未分组。bot 与管理台都会走 `migrate()`，谁先启动谁补列。

管理台每次改动后调 `_sync_config_subjects()`：以库为准回写 `config/subjects.json`（`uid/name/room_id/group`；2026-11-10 起不再写 `enabled` / `_disabled`，旧镜像里的 `_disabled` 会在下一次回写时清掉），保持「库→config」单向，不反向读。

分组名还额外登记在 `meta` 表（`k = group_names`，JSON 数组）：`groups()` 是从 `subject.grp` 汇总出来的，没有这层登记就存不住「空分组」。列表是「注册表 ∪ 库里的实际分组」，所以升级前就已有成员的老分组也能正常显示，不需要回填。

### 页面

| 区域 | 内容 |
|---|---|
| 分组筛选 | 下拉含「全部分组 / 每个自定义组 / 未分组」，命名组后缀人数与在播数（如 `PSP（12 人 · 2 在播）`），**未分组固定排最后、没有未分组主播时整项不出现**，**末尾固定一条「＋ 新建分组…」**；选它弹窗填名字（可先建空组），建成后下拉自动切到新组；切到别的项即重新拉名单并清空当前选择 |
| 名单表 | 多出「分组」列（蓝色标签，未分组的画灰色「未分组」）和行首复选框；表头复选框 = 全选当前可见行 |
| 批量条 | 有选中项时才出现：`已选 N 个` + 移入分组（下拉含「未分组」与末尾「＋ 新建分组…」，后者可边移边建组）/ 移除 / 取消选择；移除先 confirm |
| 分组管理 | **新建入口在分组筛选下拉末尾**（工具栏不放输入框了）；筛选到某个具体分组时工具栏额外出现重命名（留空 = 取消分组）、删除（成员回落未分组，不删号） |
| 添加 | 若当前正筛在某个分组，新增的主播直接进该组 |

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/subjects` | 返回 `{ok, subjects[], groups[]}`；`subjects[i].group` 为该号分组名，`groups[i]` = `{name,total,enabled,living,color}`（`enabled` 自 2026-11-10 起恒等于 `total`，仅为兼容保留），**未分组（`name=""`）固定排在最后，没有成员时不出现在数组里** |
| POST | `/api/subjects` | 新增/更新单个号，body 多了可选 `group`（筛在分组里添加时自动带上） |
| POST | `/api/subjects/batch` | body `{uids: [...], action, group?}`；`action` 取 `remove` / `group`，`group` 动作必填组名（空串 = 移回未分组） |
| POST | `/api/groups/create` | body `{name}`；新建分组（可以是空组），重名或空名 400 |
| POST | `/api/groups/rename` | body `{from, to}`；`to` 为空 = 取消分组；目标组已存在则合并；空分组也能改名 |
| POST | `/api/groups/delete` | body `{name}`；成员回落未分组 |

返回统一带 `affected`（实际影响几个号）与 `uids`（最终生效的 uid 列表，已去重排序）。

### 行为与边界

- **批量移除是一次事务**：同时删 `subject` / `subject_state` / `live_session`，不留孤儿状态；单个号不存在整批回 404，不做「部分成功」。
- **没有「停用」了**（2026-11-10 移除）：单行与批量都不再提供启用 / 停用，`PATCH /api/subjects/{uid}` 与 `action=enable|disable` 一并删除；所有号一律轮询，批量只保留移组 / 移除。
- **重命名到已存在组 = 合并**，提示「已并入已有分组」；重命名为空 = 把成员全部退回未分组。
- **删除分组不删号**，只把 `grp` 清空——避免误删 27 个号后要去截图里找 UID。
- **空分组可以存在**：新建后 `total=0` 依然出现在筛选与批量下拉里；删除空组 `affected=0`，号数不变。
- 分组名 `strip` 后最长 24 字、不能含换行；`uids` 为空、action 未知、uid 非整数都直接 400。
- 分组只影响管理台显示与批量操作，**不影响推送**（推送目标与事件过滤都不看分组）。

## 16. 名单按分组折叠展示（2026-11-06 补）

第 15 节的分组只能「筛选」，27 个号全摊开时看不出边界。这一版把名单按分组切成段，每组一行标题，可折叠。

### 页面行为

| 位置 | 行为 |
|---|---|
| 分组标题行 | 显示 ▾、组名（未分组有主播时才成一段，且永远在最后）、`N 个`、`· 在播 M`；折叠时额外显示 `已选 K`（该组里有勾选但被藏起来的号）与「已折叠，点标题展开」 |
| 点击标题 | 折叠 / 展开该组，只重绘表格，不重新拉接口 |
| 工具栏「全部展开 / 全部折叠」 | 一次处理当前可见的所有组 |
| 分组筛选 | 选「全部分组」时按组切成多段（**0 人的空组也会出现标题**）；筛到某个组时只渲染那一段 |
| 折叠记忆 | 写在 `localStorage['bili_grp_collapsed']`（组名数组），刷新、切页签、3 秒轮询重绘、重启管理台都保持 |

### 关键取舍：全选只作用于展开行

表头复选框改成「全选当前展开的分组」：折叠掉的组不会被选中。原因是有「移除」这种破坏性批量操作，若全选把看不见的号也勾上，误删了当场发现不了。已经选过再折叠的号仍保留在 `state.sel`（批量条不丢选择），此时提示写成「已选 N（含已折叠 K 个）」。

### 实现

纯前端，**没有任何接口改动**：

- `groupBuckets(subs, groups, filter, collapsed)`：按「API 返回的分组顺序 → 库里有但注册表漏了的组补在后面」切段，返回 `{name, rows, total, living, collapsed}`；空组 `rows=[]`、`total=0` 但仍然成段；**`''` 一律重排到最后，且 `rows` 为空时整段丢掉**。
- `rowHtml(s)` 从原来的内联 map 抽成单行渲染；`renderSubjects()` 拼标题行 + 展开组的行，空名单/空分组给「这个分组下没有主播」。
- `toggleGrp(name)` / `toggleAllGrp(collapse)` 改完 `state.collapsed` 后写 localStorage 并重绘；`expandedSubjects()` 是全选与 `selAll` 勾选态的取值来源。
- 组名走 `data-g="…"` + `esc()`，带引号/emoji 的组名不会破坏 `onclick`。

## 17. 分组标签颜色（2026-11-06 补）

分组标题行多了个色块，点开是常用色卡，给组名和一个 12px 色点染色；行里「分组」小标签也一起变色，扫一眼就能认组。

### 页面行为

| 位置 | 行为 |
|---|---|
| 分组标题左侧色块 | 有颜色时显示该色实心块，没颜色时是虚线空块；点一下展开/收起色卡（不会触发折叠） |
| 色卡 | 10 个常用色圆角块 + 末尾 `×`（恢复默认）；当前色有白描边 |
| 选色后 | POST `/api/groups/color` → toast → 重拉 `/api/subjects` → 组名、色点、行内标签同时变色 |
| 「未分组」 | 不显示色块（`''` 是保留段名，禁止上色） |

### 常用色卡（深色底上都够亮）

| 色值 | 观感 |
|---|---|
| `#f472b6` | 粉（默认主题色） |
| `#60a5fa` | 蓝 |
| `#4ade80` | 绿 |
| `#fbbf24` | 琥珀 |
| `#a78bfa` | 紫 |
| `#f87171` | 红 |
| `#22d3ee` | 青 |
| `#94a3b8` | 灰 |
| `#fb923c` | 橙 |
| `#2dd4bf` | 蓝绿 |

### 存储与服务端

- 颜色仍存 meta 表，键 `group_colors`，值是一张 `{"组名": "#rrggbb"}` 的 JSON；**不动 `subject` 表结构，主循环一行没改**。
- `set_group_color(name, color)`：空串 = 恢复默认（从 JSON 里删键）；只认 `^#[0-9a-fA-F]{6}$`，存前统一小写。
- `groups()` 每项多返回 `color`（没设过是 `""`）。
- `rename_group()` 把颜色带过去；并入一个**已有颜色**的目标组时保留目标色（谁被并进去就听谁的）。
- `delete_group()` 顺手删颜色；`create_group(name, color=...)` 可在新建时直接带色。
- 颜色**不写进** `config/subjects.json`——它属于展示层，主循环和 config 不需要知道。

### 接口

| 方法 / 路径 | 请求 | 说明 |
|---|---|---|
| `POST /api/groups/color` | `{name, color}` | 设色 / 空串恢复默认；成功返回 `{ok, group, color, note}` |
| `POST /api/groups/create` | `{name, color?}` | `color` 可选，带上就顺便上色 |

错误：未分组 400「不能给「未分组」上色」；格式不对 400「颜色要写成 #rrggbb，例如 #f472b6」；组不存在 404。

### 取舍

- **为什么存 meta 不存 subject**：颜色跟着「组」而不是「号」，改名时得整组搬；放一坨 JSON 里改名改一个键最省，也不用给老库加列。
- **为什么固定 10 色**：要的是「常用色卡，选一下就好」，不是取色器；固定色值还能保证深色主题下对比度够，避免有人挑个 `#3a3a3a` 看不见。
- **为什么行内标签也染色**：只染组标题的话，滚到长名单中间就分不清这行属于哪个组了；小标签带同色边框是最低成本的呼应。

## 18. 组内全选与分组拖拽排序（2026-11-06 补）

### 18.1 组内全选

| 位置 | 行为 |
|---|---|
| 分组标题行（组名左侧） | 一个复选框：勾上把**该组全部主播**加进已选，取消则整组移出已选 |
| 半选态 | 组里只选了一部分时显示横线（`indeterminate`），全选后才打勾 |
| 空分组 | 复选框禁用（没有可选的人） |
| 与表头全选的区别 | 表头全选**只作用于已展开的行**（防误删看不见的号）；组内全选是显式动作，标题写着「全部 N 个主播」，**折叠起来的组也会被整组选上** |
| 选完的反馈 | 底部批量条照常出现；提示写「已选 N（含已折叠 K 个）」，所以折叠组被选中是看得见的 |

实现：`selGroup(name, on)` 直接改 `state.sel`，数据源是 `state.subjects`（全量）而不是 `visibleSubjects()`，这样折叠状态不影响整组选择；改完 `renderSubjects()` + `refreshSubjectBar()`。复选框的 `checked` / `indeterminate` 在渲染后按 `gselState[组名]` 统一回填——组名可能是任意字符，所以用 `el.dataset.g` 取，不拼 CSS 选择器。

### 18.2 分组拖拽排序

| 位置 | 行为 |
|---|---|
| 分组标题行 | 整行可拖（`cursor:grab`），左侧有 ⠿ 把手；把手单独点了不会触发折叠 |
| 拖到别的组上 | 松手即插到目标位置：**往下拖 = 排到目标之后，往上拖 = 排到目标之前**（`splice` 先删后插天然满足两个方向） |
| 未分组 | 固定**最后一位**（且没人时不出现），不能拖，也不能被拖进去 |
| 分组筛选 | 只在「全部分组」下开放；筛到某个组时把手隐藏（组内顺序没有意义） |
| 视觉 | 拖动中的源行半透明；目标行顶部一条粉色高亮（`box-shadow: inset`） |
| 松手之后 | 先本地重排（即时反馈）→ `POST /api/groups/order`；失败 toast 并重拉名单回滚 |

顺序存在 **meta 表 `group_names` 数组的次序**里（`groups()` 就按它返回），所以刷新页面、重启服务都保持。新建的组默认排最后；提交的名单里没提到的组按原顺序跟在后面；重复项和空名忽略。

### 18.3 接口

| 方法 / 路径 | 请求 | 说明 |
|---|---|---|
| `POST /api/groups/order` | `{names: ["组A", "组B", ...]}` | 全量顺序；返回 `{ok, order, note}`；非数组返回 400「names 要是数组」 |

### 18.5 新建分组的入口调整（2026-11-06 晚）

按使用反馈，把「新建分组」从工具栏移进**分组筛选下拉的末尾**（`＋ 新建分组…`）。

| 位置 | 行为 |
|---|---|
| 分组筛选下拉末尾 | 固定一条 `＋ 新建分组…`，排在所有分组之后 |
| 选中它 | 先把下拉**复位到原来的选择**（`state.grpLast`），再弹窗问名字——取消弹窗时停在原处，不会卡在一个「假分组」上 |
| 建成之后 | toast 提示 + 下拉自动切到新组 + 重拉名单；空组也能建，建完筛到它，勾人后点「移到该分组」即可加入 |
| 与批量条的关系 | 批量条「移入分组」里的 `＋ 新建分组…`（边移边建）保留不动，两处入口行为一致 |

## 19. 单个主播的推送开关（直播 / 动态，2026-11-07 补）

### 19.1 能力

| 位置 | 行为 |
|---|---|
| 名单「推送」列 | 每个主播两个小胶囊：**直播**、**动态**。粉色=开（点击关闭），灰色=关（点击开启） |
| 直播 | 只管开播 / 下播两类事件；关掉后状态照样刷新（名单里的「直播中」还是会变），只是不产事件、不推送 |
| 动态 | 管动态（文字 / 图片 / 转发）+ 视频 + 专栏（三类同源，都从动态流派生），关掉后同样只停推送 |
| 关掉之后 | **不补推积压**：关闭期间的新动态、开播下播照样记水位 / 记已读 / 记场次，重新打开只从现在开始推 |
| 与「停用」的区别 | 2026-11-10 起名单没有「停用」了（不采集、连状态都不刷的能力已移除）；「推送关掉」是照常采集、只是不发，要静音某个号就用它 |

### 19.2 存储与迁移

- 新列在 `subject` 表：`push_live`、`push_dynamic`，`INTEGER NOT NULL DEFAULT 1`。老库启动时由 `migrate()` 用 `PRAGMA table_info` 检测后 `ALTER TABLE` 补列，**默认全开**，27/28 个老号升级后不需要手工设置。
- 之所以放库不放 `config/subjects.json`：主循环读的是库，config 只是管理台回写的镜像；放 config 会被「主循环起床时按 config 覆盖」冲掉。`_upsert_subject` 的 `ON CONFLICT` 也只写 `name/room_id`，**不碰这两个开关**（已用 `selftest_push.py` 的 [5] 用例钉住）。
- 与分组一样：管理台改完不回写 config，config 里没有这两个字段，重建环境时以库为准。

### 19.3 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/subjects` | 每个主播多返回 `push_live`、`push_dynamic` 两个布尔 |

失败语义：两个参数都不给 → 400「至少要给 live 或 dynamic 其中一个」；UID 不在名单 → 404「没有这个 UID」。

### 19.4 拦截点为什么在 watcher 而不是发送层

`core/watcher.py` 的 `round_live` / `round_dynamic` 在「造事件」这一步就判开关：

- 关直播：仍然跑完 `clear_live_session`、仍然写回 `live_status` / `last_live_start_ts`，只是 `events.append` 跳过；日志 `[直播] xxx 产出 live_start，但该主播关闭了直播通知，不入库`。
- 关动态：仍然 `mark_seen` + 水位前进，只是 `fresh.append` 跳过；日志 `[动态] xxx 有 N 条新动态，但该主播关闭了动态通知，不入库（水位照常前进）`。

放这里而不是发送层的理由：① 关掉的号不该在「事件」页留一堆永远不会发的记录；② 事件一旦入库就会入投递队列，发送层还要再过滤一次、还要处理「历史遗留 pending」；③ 水位推进在同一处，能保证语义是「不推过去的，只推今后的」。

### 19.6 已知限制

- 关闭期间的动态不会补推（这是有意为之，不是 bug）；如果确实想看「关掉期间都漏了什么」，去「事件」页看不到——事件根本没入库，只能临时打开开关从当前开始。
- 只有单号开关，没有「整组批量关推送」的入口；需要的话可以照 `/api/subjects/batch` 的写法加一个 `action=push_off`。
- 关掉直播通知不影响录播清理：录制侧读的是 `live_session` / 采集状态，不读事件表。

## 20. 单个主播的日程表（周表）监控开关（2026-11-07 补）

名单新增「日程」列：每个主播一个蓝胶囊，`监控中` / `已关闭`，点一下即时生效。

### 20.1 这个开关管什么

| 动作 | 开着（默认） | 关掉 |
| --- | --- | --- |
| 把 TA 的图文动态收进周表候选池 | 会（DRAW、1~3 张图） | **不会**，不落图、不进 `weekly_candidate` |
| 待判定批次（视觉回合要读图的清单） | 会出现在里面 | **不出现**，AI 不为 TA 花 token |
| 已经抽出来的 `weekly_schedule` 日程 | 保留 | **保留**（历史既成事实，删了会丢数据） |
| 直播 / 动态的 QQ 推送 | 不受影响 | 不受影响（那是「推送」列的两个胶囊） |

一句话：这是「要不要识别 TA 的周表」，不是「推不推通知」。

### 20.2 存储与迁移

- `subject.weekly_on INTEGER NOT NULL DEFAULT 1`；老库由 `store/sqlite.py` 的 `migrate()` 用 `PRAGMA table_info(subject)` 检测后 `ALTER TABLE` 补列，默认全开（升级后 28 个号都是 `监控中`）。
- `config/subjects.json` 不参与：`_upsert_subject` 的 `ON CONFLICT` 只写 `name` / `room_id`，主循环起床不会把用户关掉的开关冲掉。
- 删号再重加会回到默认「开着」（新行 `weekly_on=1`）。

### 20.3 接口

`POST /api/subjects/flags` 现在收三个键，**至少给一个**，否则 400：

| body | 效果 |
| --- | --- |
| `{"uid":1,"live":false}` | 关直播推送 |
| `{"uid":1,"dynamic":false}` | 关动态推送 |
| `{"uid":1,"weekly":false}` | 关日程表监控 |
| `{"uid":1}` | 400：至少要给一个 |
| uid 不存在 | 404 |

回读：`GET /api/subjects` 每行多一个 `"weekly": true/false`。服务端方法名从 `set_subject_push` 改成 `set_subject_flags`（`scripts/selftest_push.py` 的调用已同步）。

### 20.4 拦在哪两层，为什么

- **L1（采集侧）**：`core/weekly.py` 的 `scan()` 开头取一次 `weekly_off_uids(conn)`，命中就 `continue`——不下载图片、不建候选行。日程进程组每 10 分钟的周表扫描自动按新口径跑。
- **L2（判图侧）**：`pending_batch()` 过滤掉关闭的主播；新增 `pending_count(conn)` 用 `uid NOT IN (...)` 统计，`scripts/weekly_ai.py` 的 gate/status 与 `web/console.py` 的 `/api/weekly/ai` 都改成调它，所以关掉的号既不进批次、也不占「待判定 N 条」。
- 为什么不只在 L2 拦：候选落盘会把图下载到 `data/schedules/<uid>/`，先把没意义的图下下来再丢掉，是白花带宽和磁盘。

### 20.6 已知限制

- 只有单号开关，没有「整组批量关日程监控」；需要可照 `batch_subjects` 加 `action=week_off`。
- 关掉期间漏掉的图文**会补**：`scan()` 回看 `event` 表最近 3 天，重新打开后这 3 天内的图在下一轮收进来；超过 3 天的不再补。
- 已上表的日程不会因为关掉而消失（`week_view` 照常读 `weekly_schedule`），这是有意的。

## 21. 已判定候选图的缓存清理（2026-11-07 补）

### 21.1 页面上多出来的是什么

日程表页「待确认候选」那一行的右边：

- `本地图缓存 113 张 / 139.3 MB` —— 由 `GET /api/weekly` 新增的 `cache` 字段驱动，随页面刷新更新。
- `清理已判定缓存` 按钮 —— 手动触发一次清理（先 `confirm`，成功后 toast「已清理 N 张，释放 X」并重载日程表）。

### 21.2 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/weekly` | 返回体新增 `cache: {files, bytes, mb}` |
| POST | `/api/weekly/cache/clean` | body `{"force": false}`；`force=true` 时连 `weekly_cache_keep_days` 的缓冲也不给，直接按 0 天清 |

返回体就是 `purge_cache()` 的统计，另外附一份清理后的 `cache` 和 `ok: true`：

```json
{"scanned": 3, "removed": 0, "bytes": 0, "kept_pending": 2, "kept_ref": 1,
 "cleared_candidates": 0, "cleared_sessions": 0, "dirs_removed": 0,
 "cache": {"files": 3, "bytes": 4804104, "mb": 4.6}, "ok": true}
```

### 21.3 什么时候会自动清

不只是按钮——三个入口都会清，按钮只是「现在就想清干净」：

1. `scripts/weekly_ingest.py` 判定落库后立刻清（AI 回合判完就释放）。
2. 日程进程组每 10 分钟的周表扫描兜底一次，日志出现 `[周表] 缓存清理：删除 N 张（X MB），保留待判定 N 张、近期待用 N 张`。
3. 管理台按钮。

### 21.4 取舍

- **待判定的图一张都不动**：AI 与人工都要看图定性，删了等于把队列废掉。
- **判定完的只保留 `url` 不保留图**：候选行还在（历史可查、可改判），改判时 `set_candidate_scope` 会调 `ensure_images()` 按 `url` 重下。
- **直播预约要用的图按场次日期保护**：`weekly_session.image_path` 在场次过去之后才允许删，避免「预约卡还没推，图先没了」。
- **`inbox/` 一律不碰**：那是视觉回合的收发目录，不是缓存。

## 22. 进程页：后台任务与独立进程的开关（2026-11-08 补）

### 22.1 这一页解决什么

以前要停某个后台动作，只能改代码、改配置再重启整个服务；独立进程（弹幕监控台、录播清理守护）更是只能去点 bat / vbs。现在多了一个「进程」页签，把主进程以外的东西都摊成一屏，能看到状态、能开关，**改完不用重启**。

> 2026-11-08 起日程功能默认关闭：关闭时 GET /api/tasks 不下发 worker 分组，这一页看不到「日程进程组」与那 4 个循环；开启后自动出现（见 §24.0）。schedule_worker 本身仍只读。

| 分类 | 条数 | 这些东西跑在哪 | 开关的语义 |
| --- | --- | --- | --- |
| 主进程内任务（loop） | 6 | 就在 `run_bot.py` 的 asyncio 循环里 | 暂停 = 这一轮不干活，进程照活；落库，重启后仍然暂停 |
| 日程进程组（worker） | 5 | 独立进程 `scripts/schedule_worker.py`，由守护拉起和重启 | 4 个循环可单独暂停；进程组本身只读，要停用 `service.ps1 stop` |
| 独立进程（process） | 5 | 采集进程、推送进程（由 `web/stack.py` 托管，见 §26）、弹幕监控台（127.0.0.1:8760）、录播清理守护、守护进程 | 前四个可启动 / 停止 / 重启，并各带一个「开机自启」开关（§22.11）；守护只读 |
| 主进程（self） | 1 | 就是你正在看的这一页 | 可重启 / 停止；停止要二次确认，见 §22.9 |

6 个主进程循环：直播采集、动态采集、投递派发、缓存清理、QQ 收件、清理通知。

日程进程组 5 项：周表扫描（含候选图缓存清理）、直播预约刷新、日程识别交接（`weekly_agent`，导出/回收任务）、每日日程推送、进程组本身。

### 22.2 开关存在哪、多久生效

- 落库：`meta['tasks_off']`，值是 key 的 JSON 数组（如 `["dynamic","live"]`）。**不写 config**——config 只是名单镜像。
- 循环侧：`core/tasks.py` 的 `Board.off_keys()` 带 **1 秒 TTL 缓存**，所以点完最多一两秒就停；`Watcher._sync_tasks()` 每轮读一次，只在状态翻转时打一行日志：

  ```
  [任务] 动态采集 已暂停（管理台「进程」页可改）
  [任务] 动态采集 已恢复（管理台「进程」页可改）
  ```

- 主进程循环的运行态（最近心跳、计数）只在内存里，进程重启就清零；用 `scripts/run_console.py` 单独跑管理台时，这些行会显示「未运行」（没有主循环写心跳）。
- 日程进程组的心跳与每个任务的计数写在 `meta['worker_state']`，跨进程可读，管理台单独跑也能看到进程组最后一次心跳（超过 2 分钟没更新或 pid 不在了就算未运行）。

### 22.3 判定「活着」的依据

| 行 | 判据 |
| --- | --- |
| 主进程循环 | 看主循环心跳 `meta['last_tick']`：3 分钟内有更新才算主进程活着（再叠加自身被暂停的状态） |
| 日程进程组循环 | 看 `meta['worker_state']` 的 `last_beat`（2 分钟内）与 `pid` 是否存活，再叠加自身被暂停的状态 |
| 弹幕监控台 | socket 连 `127.0.0.1:8760`，0.4 秒超时；连上就是活着，顺带从 `netstat` 找监听 PID |
| 录播清理守护 | `CleanupManager.running_pid()` + OpenProcess/GetExitCodeProcess 判活 |
| 守护进程 | 读 `data/supervisor.json` 的 `pid` / `child_pid` / `worker_pid` 再判活 |
| 监控台主进程 | 就是本进程（`os.getpid()`） |

### 22.4 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/tasks` | 任务状态（含 `off`、`bot_alive`、`autostart`）；每行带 `autostart` / `autostart_text`（§22.11）；日程功能关闭时 worker 组整个不下发，正常是 loop/worker/process/self 四组 |
| POST | `/api/tasks/toggle` | body `{key, off}`：暂停 / 恢复一个循环任务 |
| POST | `/api/tasks/process` | body `{key, action}`，action = `start` / `stop` / `restart`，只对独立进程有效 |
| POST | `/api/tasks/autostart` | body `{key, on}`，其中 `on` 为布尔值。`main` 设置下次 Windows 登录时是否启动监控台，其余四个独立进程设置是否随监控台启动（§22.11）。 |
| POST | `/api/tasks/main` | body `{action, confirm?}`：主进程的 `restart` / `stop`；`stop` 必须带 `confirm: true`。托管与独立运行的分流见 §22.9 |

拒绝规则（都是 400，说清原因）：未知 key；`toggle=False` 的 schedule_worker / supervisor / main；对 loop / worker 调启停；对 supervisor 调启停（提示改用 `scripts/service.ps1 stop`）；对 main 传 `stop` 但没带 `confirm: true`（停止要二次确认）。

### 22.5 几个刻意不做的事

1. **主进程只给「重启」和「带二次确认的停止」，日程进程组与守护仍只读**：主进程的两个动作按托管 / 独立运行分流（§22.9）；进程组与守护是整条链路，要停请用 `service.ps1 stop`（先写停止文件，两个子进程自己退）。
2. **暂停 ≠ 停用**：暂停只影响那一个循环，其它循环照常；要停整条流水线用概览页的「全停」总开关。
3. **不在这里另装 Windows 自启项**：本页的「开机自启」是「随监控台启动」（§22.11），不是计划任务；监控台自身的开机自启仍归 `scripts/service.ps1`（计划任务或启动文件夹）。日程进程组由 `supervisor.py` 自动拉起和重启；livedeck 与清理守护的手动启停仍在这一页。
4. **不做进程级 CPU / 内存占用**：没有引入 psutil，避免为一行数字加依赖。

### 22.7 「监控室」入口与「关于」弹窗（2026-11-08 补，2026-11-10 挪进「设置」菜单，同日补「关于」）

管理台顶栏最右侧是一个**齿轮设置按钮**（`#settings-btn`，带 `aria-haspopup="menu"` / `aria-expanded` / `role="menu"`），点开四项菜单，点空白处或按 Esc 关闭：

| 菜单项 | 行为 |
| --- | --- |
| 监控室 | 新标签页打开 `http://127.0.0.1:8760/` —— 多屏直播 + 合并弹幕那个页面（见 [23-多屏直播监控台](23-多屏直播监控台.md)）。它和管理台是两个独立服务，所以是外链不是页签 |
| 网络代理 | 打开「网络代理」弹窗（见 §22.13），右侧小字跟着配置显示「未设置 / 已开启」 |
| 更新检查 | 打开「更新检查」弹窗（见 §22.10），标「与 Gitee 比对」 |
| 关于 | 打开「关于」弹窗（见下），标「版本信息」 |

监控室舞台通过自身的 `GET /api/playback` 选择 FLV 或 HLS，参数和返回字段见 [监控室路由表](23-多屏直播监控台.md#6-http-接口)。

2026-11-10 之前这个入口是导航栏右下角单独的一条「监控室 ↗」链接，现在**已取消**，只留菜单里的「监控室」，避免两个入口并列。

| 情况 | 怎么办 |
| --- | --- |
| 点开是空白 / 连接被拒 | livedeck 没在跑 —— 到「进程」页点 `livedeck` 那行的「启动」，起来再点 |
| 已在跑 | 「进程」页 `livedeck` 那一行也会多一个「打开」按钮，不用回顶栏 |

- 链接写死 `127.0.0.1:8760`：管理台只监听环回，别的机器本来就访问不到，不做成可配置域名。
- 想改端口：`run_livedeck.py --port`，改完链接与「进程」页探测的 `LIVEDECK_PORT` 一起改（`web/console_tasks.py`）。
- 与监控室那边对称：监控室顶栏的「设置 → 监控台」也是写死 `127.0.0.1:8765`，两个页面互相跳转。

### 22.8 「关于」弹窗（2026-11-10 补）

「设置 → 关于」打开 `#aboutModal`（`.mbox.about-box`，520px、窄屏 93vw、最高 82vh 后可滚动），内容与监控室那份完全一致，只有字号随本页主题略大：

| 区块 | 内容 |
| --- | --- |
| 标题行 | 「DD全能工作间」+ 「版本 vX.Y.Z · MIT 许可」；版本号先渲染占位「—」，打开时向 `GET /api/version` 取一次再填 |
| 首段 | 一句话说明：本机常驻、盯关注的主播、开播/下播/动态、卡片图、QQ 机器人推送，附监控台与监控室两个界面 |
| 三条要点 | 数据只在本机（`data/`、`logs/`、`out/`、`config/*.env`）、只监听环回、非官方工具（与哔哩哔哩、腾讯无关联） |
| 开发者块 | 开发者 VictorKiseki · 邮箱（纯文本不可点，可选中复制）；源码 → `https://gitee.com/victorkiseki/bili-monitor`；反馈 → 同仓库 `/issues`，两条链接都新标签页打开 |
| 页脚 | Python 3.10–3.12（64 位）· Windows 10 1809 及以上；mpegts.js 1.8.0（MIT）· © 2026 VictorKiseki |

- 关闭：右上角 `#aboutClose`、点遮罩空白处、Esc 三处都行；Esc 与另外两个弹窗共用同一条 `keydown` 链（先关清理弹窗/进度，再关关于，最后收设置菜单）。
- **版本号不写死**（AGENTS §1 R5）：页面里没有任何 `x.y.z` 字面量，只走新加的 `GET /api/version` → `ConsoleViews.version()` → `shared.version.read_version()`，唯一来源仍是仓库根目录的 `VERSION`。
- 关于里不再放更新检查按钮，入口只在设置菜单那一项（§22.10），避免重复。
- 文案与监控室那份同一套：改文案要两侧一起改（`web/index.html` 与 `livedeck/static/index.html`），`tests/test_about_dialog.py` 会同时断言两边。

| 接口 | 返回 |
| --- | --- |
| `GET /api/version` | `{"ok": true, "version": "0.2.0"}`，值取自 `VERSION` |

### 22.9 主进程的停止与重启（2026-11-10 补）

主进程那行原来是「只读」，要重启整条服务只能去命令行。现在给两个动作；停止**不可撤销**，所以设计成两步：

| 动作 | 页面行为 | 后端动作 |
| --- | --- | --- |
| 重启 | 一次点击；随后换成「正在重启监控台主进程…」卡片，并自动重试拿状态 | 托管：写 `data/restart.request`；独立：拉起 `scripts/restart_console.ps1`（先结束本进程，再按记下的原命令行用 pythonw 拉起） |
| 停止 | 第一次点只把按钮换成「确认停止 / 取消」（15 秒内不点自动撤回；列表每 3 秒整表重绘，确认态存在全局所以不会被冲掉），第二次点才真发请求 | 托管：写 `data/supervisor.stop`（主进程、日程进程组、守护一起优雅退出）；`run_bot.py` 独立运行：写 `data/bot.stop`；`run_console.py` 独立运行：延迟 0.8 秒关掉 HTTP 服务器 |

停止之后这一页会停掉 3 秒轮询、把表格换成「监控台已停止」卡片，并写清怎么回来：项目根目录执行 `scripts\restart_console.ps1 -Start`，托管时也可以 `scripts\service.ps1 start`。

托管还是独立，看 `data/supervisor.json` 里那个守护的 pid 是否还活着，**并且本进程（或它的 `.venv` 启动器父进程）就是它拉起来的那个**：另开的临时管理台（换端口 / 临时库）不会被误判成托管，免得一按停止把真正在跑的那套服务停掉。`GET /api/tasks` 的 self 行现在带 `managed`（是否托管）与 `port`（真实监听端口，不再是写死的 8765），表格里也会写「由守护托管 / 独立运行」。

验证（2026-11-10）：

- `tests/test_tasks_main_control.py` 14 个用例全绿：action 白名单、stop 缺 confirm 不落任何文件、托管写 `restart.request` / `supervisor.stop`、守护在跑但不是自己的父进程时按独立处理（不写 `supervisor.stop`）、独立重启调 helper（端口取自 `_web_port`）、`run_bot` 写 `bot.stop`、`run_console` 关服务器、行里 `port` 与 `managed`、路由/页面/文档的接线静态断言。
- 页面 `<script>` 抽出来 `node --check` 通过。
- 托管实测重启：守护 pid 不动、`child_pid` 换新、端口重新监听（过程细节见 [13-守护与开机自启](13-守护与开机自启.md) §9.4）。

### 22.10 「更新检查」弹窗（2026-11-10 补）

「设置 → 更新检查」打开 `#updModal`（`.mbox.upd-box`，430px、窄屏 93vw），只做一件事：拿仓库里的 `VERSION` 和本机比一下，**不下载、不升级、不改任何文件**。

| 状态 | 页面显示 |
| --- | --- |
| 发现新版本 | 「发现新版本 vX.Y.Z，可到仓库看更新说明」（绿色进度色），下面两个按钮：打开 Gitee 仓库 / 关闭 |
| 已是最新 | 「已是最新版本」（正常色），当前版本与最新版本两行都填上 |
| 失败 | 「检查失败：HTTP 403」这类原文（红色），当前版本仍显示，最新版本留「—」 |

| 区块 | 内容 |
| --- | --- |
| 标题行 | 「更新检查」+ 右上角 `#updClose` |
| 状态行 | `#updStatus`，打开瞬间先写「正在检查…」，请求回来再换成上面三种之一 |
| 版本对照 | 当前版本 `#updCur`（来自 `GET /api/version` 同一个 `read_version()`）与最新版本 `#updLatest` |
| 说明 | 一句话：只读仓库里的 `VERSION`，不会下载或替换文件；要更新就自己去仓库取 zip |
| 底部 | 「打开 Gitee 仓库」（新标签页）+「关闭」 |

- 关闭：`#updClose`、`#updOk`、点遮罩空白处、Esc 四处都行；Esc 链顺序是 清理弹窗 → 进度 → 关于 → 更新检查 → 设置菜单。
- 比对在**服务端**做，不经浏览器跨域：`GET /api/update` → `ConsoleViews.update()` → `shared.update.check()`；按 `shared.update.REFS` 的顺序试 `raw/opensource/VERSION`、`raw/master/VERSION`、`raw/main/VERSION`，6 秒超时，失败只返回 `error` 不抛异常。
- **`opensource` 排第一**：它是发布线（只收整批确认的快照），`master` 是开发线、可能带着还没发的改动，所以更新检查跟「已发布的版本」比；后两个只在发布分支改名时兜底。
- 仓库已公开；`/raw/<分支>/VERSION` 会 302 到 `raw.giteeusercontent.com`，`check` 用的是带 `follow_redirects=True` 的客户端。若分支名不对或仓库里没有 `VERSION`，错误写「仓库里没有 VERSION（HTTP 404，试过 opensource / master / main）」而不是裸 `HTTP 404`。
- 不把 token 塞进前端：前端只发一个本机请求，跨域与鉴权都在服务端。
- 源码与反馈链接指向同一个仓库：`https://gitee.com/victorkiseki/bili-monitor` 与 `/issues`；改仓库地址要同时改 `shared/update.py` 的 `GITEE_REPO` 和两侧页面里的 `href`（`tests/test_update_check.py` 会断言）。

| 接口 | 返回 |
| --- | --- |
| `GET /api/update` | `{"ok": true, "current": "0.3.0", "latest": "0.3.0", "has_update": false, "url": "https://gitee.com/victorkiseki/bili-monitor", "error": ""}`；失败时 `ok:false` 且 `error` 为可读中文 |

### 22.11 进程页的「开机自启」（2026-11-11 补）

进程页每行多了一列**开机自启**：前面 4 个独立进程（`collector` / `pusher` / `livedeck` / `cleanup_daemon`）各有一个勾选框，勾上就是「随监控台启动」——下次 `run_console.py` 起来时顺手把它拉起来，不勾就不拉。

- **监控台主进程**：在「开机自启」列勾选后，守护与监控台会在下次登录 Windows 时启动。已有的 `BiliMonitor` 计划任务可直接启用或禁用，首次从页面开启时会在当前用户的启动文件夹建立 `BiliMonitor.vbs`。取消勾选会禁用本安装的计划任务并移除启动文件夹项，当前运行的服务继续运行。守护进程行显示同一个自启状态。
- **落库**：`meta['process_autostart']`，值是一个 JSON 对象，**只保存与默认值不同的 key**（如 `{"livedeck": true}`）。默认值在 `core/tasks.py` 的 `AUTOSTART_DEFAULTS`：`collector`、`pusher` 默认开（现状就是绑上账号就起，升级后行为不变），`livedeck`、`cleanup_daemon` 默认关（以前要手动点「启动」，不能升级完凭空多出两个进程）。清空勾选就是把该 key 从对象里删掉，回到默认。
- **只影响下次启动**：点勾立刻落库并回一句提示，**不动当前正在跑的进程**——关掉开关不会把活着的进程当场停掉，要停请用同一行的「停止」。所以页面文案写「下次启动监控台时生效」。
- **应用时机**：`run_console.py` 建好 `StackManager`、赋给 `console.stack` 之后、`stack.start()` **之前**调 `console.apply_autostart()`。采集/推送交给 `StackManager.set_autostart()`（`desired()` 的优先级：手动 on > 手动 off > 自启关 > 绑定命中），弹幕监控台与清理守护为真的才拉起；清理守护还要先看功能本身是开着的（`cleanup.config()["enabled"]`），避免顺手把清理功能打开。整个过程 try/except 吞错，任何一个起不来都不能挡住页面。
- **只读行**：`schedule_worker` 显示「随守护启动」（由 `supervisor.py` 拉起）；loop / worker 里那些进程内任务显示「—」，它们不是独立进程，没有自启这回事。

| 行 | 勾选框 | 状态文案 |
| --- | --- | --- |
| 采集进程 / 推送进程 / 弹幕监控台 / 录播清理守护 | 有 | 「随监控台启动」/「不自动启动」 |
| 监控台主进程 | 有（状态可读取时） | Windows 登录自启（计划任务 / 启动文件夹 / 未设） |
| 守护进程 | 无 | 与主进程共用的 Windows 登录自启状态 |
| 日程进程组本身 | 无 | 随守护启动 |
| 6 个循环、4 个日程循环 | 无 | — |

`GET /api/tasks` 的顶层 `autostart` 返回四个子进程开关，主进程行的 `autostart` 和 `os_autostart` 返回 Windows 登录自启状态。`POST /api/tasks/autostart` 接收 `{key, on}`。成功时返回 `{ok, key, name, on, note}`，子进程另带 `autostart`，主进程另带 `os_autostart`。主进程状态缓存 60 秒，通过页面切换后立即更新。

### 22.12 概览页与取数方式的两处提示（2026-11-11 补）

- **推送目标空态**：概览的「推送目标」表格在 `targets` 为空时不再只写一行说明，而是「还没有推送目标：先到「机器人」页把 QQ 机器人的 openid 绑上，绑好并重启采集进程后这里会自动出现。」+ 一个「去机器人页绑定 openid」按钮。按钮走 `gotoQQBind()`：切到「机器人」页签，再平滑滚到 QQ 绑定卡片（`#qqBindCard`），省得用户自己找。
- **取数方式下拉多了「自动」**：概览「动态采集方式」卡片的下拉以前只有「关注流 / 逐人轮询」两项，而配置里的默认值是 `auto`——页面把它硬显示成「关注流」，用户选了别的又被每 3 秒一次的轮询覆盖回去，看起来像「自动跳回关注流」。现在：
  1. 第一项是 `auto`「自动（有登录凭据时走关注流）」，与 `config/subjects.json` 的 `poll.mode` 一一对应；
  2. 改动下拉只是把「切换（需重启）」按钮点亮并显示「选项已改动，点「切换」才保存」，**不自动保存**，也不会被定时刷新覆盖（`feedModeDirty` 标记）；
  3. 保存成功后才清掉标记；页面提示里同时写清「配置为 X、实际跑 Y」（`mode_effective`，例如配置 `feed` 但 SESSDATA 不全时会退回逐人轮询）。

  两种设置都不改变「实际采集方式」本身：改下拉不发任何请求，真正切换仍要点按钮并重启采集进程，见 [35-关注流采集](35-关注流采集.md) 第 2 节。

### 22.13 网络代理设置（2026-11-12 补，2026-11-12 改三模式）

「设置 → 网络代理」打开 `#proxyModal`（`.mbox.proxy-box`，470px），监控室那边是同一套字段、同一份配置，两个页面改的是**同一个文件**：

| 区块 | 内容 |
| --- | --- |
| 模式 | 单选三选一：不使用代理（`#proxyModeNone`）/ 使用系统代理（`#proxyModeSystem`，默认）/ 自定义代理（`#proxyModeCustom`） |
| 地址 | `#proxyUrl`，形如 `http://127.0.0.1:7890`；只有「自定义代理」时才可编辑，只接受 `http://` / `https://`，可带 `user:pass@` |
| 状态行 | `#proxyStatus`：读配置中 / 已保存 / 保存失败 / 连接成功（HTTP 200，xx ms）/ 连接失败（原因） |
| 按钮 | 测试连接（`#proxyTest`，服务端用当前模式真的访问一次更新检查地址）、保存（`#proxySave`）、取消（`#proxyCancel`） |

三种模式各做什么：

| 模式 | 环境变量 | 适用场景 |
| --- | --- | --- |
| 不使用代理 `none` | 主动删掉 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`（含小写），系统里设了也不走 | 本机有系统代理但 B 站 / Gitee 想直连，或代理坏了要临时绕开 |
| 使用系统代理 `system`（默认） | 完全不动，沿用系统 / 启动脚本里已有的代理 | 平时就走系统代理（Clash 之类）的用户，不用在这里再填一遍 |
| 自定义代理 `custom` | 把 6 个代理变量写成下面填的地址，`NO_PROXY` 写成 `no_proxy` | 只让本程序走某个特定代理，或系统没设代理时临时指定 |

- 配置存 `config/proxy.json`（`mode` / `url` / `no_proxy`），真实文件在 `.gitignore` 里，模板是 `config/proxy.json.example`；`scripts/preflight.py` 会校验它。旧版只有 `enabled` / `url` 的配置文件仍能读：开着且填了地址按 `custom`，其余按 `system`。
- 切换模式不会清掉已填的地址：选「不使用 / 使用系统」时 `url` 照旧存着，切回「自定义代理」不用重敲。
- **怎么生效（2026-11-20 改，以前要手动重启）**：服务端保存后立刻调用 `shared/proxy.py` 的 `apply_to_env()`——`custom` 写入代理变量、`none` 删掉代理变量、`system` 还原成进程启动时看到的值。项目里所有 httpx / urllib / websocket-client 出口都默认 `trust_env`，所以**本进程新的请求立即生效**。
  - 采集 / 推送：`StackManager` 每轮对账（默认 2 秒）比对 `config/proxy.json` 的指纹（`shared/proxy.py` 的 `revision()`，即 `mtime_ns` + 文件大小），一变就先 `apply_to_env()` 再重启在跑的子进程，新子进程继承新环境；日志里是 `[栈] 代理设置已变更，重启 …`。没在跑的列不重启，下次启动自然生效。
  - 监控室：进程内每 5 秒看一眼同一个指纹，变了就调 `Deck.apply_proxy()` 重建三个连接池（`stream_http` / `http` / `BiliClient`），不重启页面、不断舞台；还挂着的旧直播流会报错并由前端自动重连。
  - 日程进程组（`scripts/schedule_worker.py`）启动时也调一次 `apply_to_env()`；周表抓图的模块级连接池（`core/weekly/fetch.py` 的 `_http()`）按同一个指纹重建。
  - 起因：系统里的代理软件崩溃后没再起来时，进程会一直往死端口上撞（日志里 `[WinError 10061] 由于目标计算机积极拒绝`），用户在页面上选「不使用代理」却要手动重启才生效；现在保存完几秒内自己换过来。
- **不覆盖的地方**：浏览器里的直播画面与弹幕（走本机 Edge 的 CDP 桥）、QQ 收消息用的 `websockets`（库本身不支持 HTTP 代理）不走这个代理。`no_proxy` 默认 `127.0.0.1,localhost,::1`，本机 8765 / 8760 / CDP 端口不会被绕去代理。
- **本机豁免只增不减**（2026-11-14）：手工在 `config/proxy.json` 里写 `no_proxy` 时，`shared/proxy.py` 的 `merge_no_proxy()` 会把它**追加**在 `127.0.0.1,localhost,::1` 后面去重，而不是整串替换——以前填一个 `example.com` 就把三个本机地址冲掉，监控台自己调 8760 / CDP 的请求会被送进代理。
- **只支持 http(s)**：httpx 走 socks5 需要额外的 socksio 依赖，按 AGENTS §5「不新增运行时依赖」明确拒绝，填 `socks5://` 会直接报错并说明原因。
- 切回「使用系统代理」时只还原本进程自己改过的变量（`shared/proxy.py` 里先拍快照再写），不会把系统里原有的代理设置冲掉。
- 「使用系统代理」下点「测试连接」，如果 `HTTP_PROXY` / `HTTPS_PROXY`（含小写）全是空的，会直接提示「系统里没有配置 HTTP(S) 代理」，不会假装成功。

| 接口 | 返回 |
| --- | --- |
| `GET /api/proxy` | `{"ok": true, "mode": "system", "mode_name": "使用系统代理", "enabled": false, "url": "", "no_proxy": "127.0.0.1,localhost,::1", "active": false}`（`enabled` 仅为兼容旧前端，只有 `custom` 且填了地址才是 true） |
| `POST /api/proxy` | body `{mode, url}`；模式非法 / 自定义却没地址都 400 并给出原因；成功返回状态 + `note`（本进程立刻换用；采集 / 推送几秒内自动重启） |
| `POST /api/proxy/test` | body `{mode, url}`（缺省用已保存的）；`none` 直接回「当前是「不使用代理」，不用测试」；`system` 按系统代理测；返回 `{ok, passed, status, ms, target, error}`，永远不抛异常 |

### 22.14 配置迁移

在原机器的监控台打开「设置 → 配置迁移」，点击「导出配置」。迁移包包含监控名单及分组、主播推送与日程开关、采集与日程识别设置、进程随监控台启动的选项、主播别名和常驻节目，以及代理、录像清理配置和监控室布局。

需要迁移 B 站和 QQ 的登录资料、QQ 推送目标、现有 AI 凭据文件或带认证的代理时，勾选同时导出凭据。含凭据的 JSON 为明文，仅在信任的设备间传输。

在目标机器先停止监控室，再从同一菜单选择「导入配置」，确认覆盖。包里的名单、开关和配置会替换目标机器对应设置，包里缺少的独立配置文件会保留本机版本。完成后重启监控台与监控室，并核对录像目录和代理地址。事件和日程历史、缓存、浏览器本地偏好、Windows 登录自启项及计划任务保存在各自的设备。

| 接口 | 行为 |
|---|---|
| `POST /api/profile/export` | 接收 `{include_private: false}`，返回版本化 JSON 附件。 |
| `POST /api/profile/import` | 接收原始迁移包 JSON，上限 8 MiB，返回导入的名单和文件数量。 |

## 23. 「机器人」页：QQ 绑定全在管理台里做（2026-11-08 补）

原来绑定 QQ 机器人只有命令行（\`setup_wizard.py --qq\` / \`p0_check.py listen\`），交接给别人时最容易卡在这一步。现在「机器人」页把整条链路做成了四个按钮。

### 23.1 页面

| 卡片 | 内容 |
|---|---|
| QQ 官方机器人 | AppID / AppSecret 输入框、「保存并校验」「用已保存的凭据重新校验」「重启采集进程」，右上角状态点（未配置 / 凭据已存缺 openid / 已接入） |
| 绑定推送目标（openid） | 「开始监听」（默认 180 秒，可停）、实时进度（连网关 → 等你发消息 → 收到）、私聊 / 群 openid 回显、「保存 openid」「清除 openid」 |
| 发一条测试消息 | 选私聊/群 + 文本，直接验证 token → 发送 → 手机 整条链路 |

### 23.2 接口

| 方法 / 路径 | 请求 | 说明 |
|---|---|---|
| \`GET /api/qq/status\` | — | 凭据是否配置（只回掩码）、env 文件路径与来源、openid、\`ready\`、\`need[]\`、监听状态、\`need_restart\` |
| \`POST /api/qq/credentials\` | \`{app_id, secret}\` | 空字段沿用已存值；写 \`BBOT.env\`（权限 600、注释保留、原子替换）后自动校验；**页面不回显明文** |
| \`POST /api/qq/verify\` | \`{app_id?, secret?}\` | 缺省用已存凭据；失败 400 并把平台的错误码翻译成可操作提示 |
| \`POST /api/qq/listen\` | \`{timeout}\` | 10–900 秒，默认 180；已在跑返回 \`already: true\`；后台线程连网关 |
| \`GET /api/qq/listen\` | — | \`{running, stage, events, user_openid, group_openid, elapsed, last_event, hint, error}\` |
| \`POST /api/qq/listen/stop\` | — | 手动停止（页面每 1 秒轮询一次状态） |
| \`POST /api/qq/ids\` | \`{user_openid?, group_openid?}\` | 手动粘贴兜底：必须是 32 位十六进制，统一大写后写 \`.p0_ids.json\` |
| \`DELETE /api/qq/ids\` | — | 只留 \`_说明\` 一行 |
| \`POST /api/qq/test\` | \`{kind, content?}\` | 用当前凭据与已存 openid 实发一条；403/404 会提示「群未开主动推送 / 不在开发体验白名单 / 机器人未入群」 |

公共实现是 \`channels/qq_setup.py\`（凭据读写、token、发送、\`OpenidListener\`），命令行 \`scripts/p0_check.py\` 只是它的薄壳——两条入口不会各写一套 HTTP 细节。

### 23.4 取舍

- **openid 仍然存文件**（\`.p0_ids.json\`）而不是数据库：主推送进程与守护脚本都直接读它，改成表要多改三处且没有收益；它已在 \`.gitignore\` 里。
- **凭据只存 \`BBOT.env\`**：与命令行共享同一份配置，避免「管理台改了但后台服务读的是另一个文件」。
- **监听默认 180 秒**：够用户切到 QQ 发一句话，又不会把线程一直挂着；\`timeout=0\` 的无限等待只留给命令行。

为什么这么改：工具栏原来一行里塞着筛选、人数提示、展开/折叠、输入框、按钮、重命名、删除，窄屏会折行；而下拉本身就是「切分组 / 加分组」的地方，把新增放末尾更顺，也省掉一个常驻输入框。代价是多一次点击（先选菜单项再填名）。

实现：`renderGroupOptions()` 在拼 `#grpFilter` 选项时追加 `＋ 新建分组…`；`#grpFilter.onchange` 判到 `__newg` 就复原成 `state.grpLast` 再调 `createGroup()`；`createGroup()` 改为 `prompt()` 取名，成功后同步 `state.grpLast`。工具栏的 `#grpNew` 输入框与 `#grpCreate` 按钮已删除。

### 18.6 未分组排到最后、没人时不显示（2026-11-06 晚）

| 位置 | 行为 |
|---|---|
| 名单 | 未分组段不再固定第一，**排在所有命名组之后**；没有未分组主播时整段不画（不再是「未分组 0 个」的空壳） |
| 筛选下拉 | 未分组选项同样排最后，同样没人就消失；命名组顺序不变 |
| 拖拽 | 未分组仍不可拖、也不能作为落点；命名组之间的顺序照旧 |
| 服务端 | `groups()` 不再无条件返回 `""` 项，只有 `total > 0` 时才追加到数组末尾；`/api/subjects` 的 `groups` 因此可能只有命名组 |
| 前端兜底 | `groupBuckets()` 把 `""` 重排到最后、`rows` 为空时丢掉；`grpDrop()` 重排本地 `state.groups` 时也把 `""` 追加到末尾 |

验证（2026-11-06 晚，重启后守护 21744 / 子进程 24012）：

- `selftest_groups.py` 52 → **56 条**断言全过（新增「未分组没成员时整段不返回」「挪走后目标组人数变多」）。
- `node --check` 通过；服务端 HTML 含 `未分组（` 与 `＋ 新建分组…`。
## 24 日程识别

> 日程功能默认关闭。在「监控名单」添加框输入 `weekly_open` 开启，输入 `weekly_close` 关闭。
> 日程表「日程识别」可选择 Harness 文件交接、自定义 Agent API 或 DeepSeek 直连。接入契约见 [33-日程识别](33-AI日程识别.md)。

后端分三层：`core/weekly/agent.py`（闸门、导出、回收、清洗）、`core/weekly/harness.py`（任务/结果文件契约）、
`web/console_weekly.py`（HTTP 数据层）；常驻循环在 `scripts/schedule_worker.py` 的 `weekly_agent_loop`，
命令行走 `scripts/weekly_agent.py`（导出/回收）与 `scripts/weekly_ai.py`（闸门/人工复核）。

### 24.0 总开关（默认关闭）

`meta['weekly_feature_enabled']` 是唯一事实来源：没有这个键就是关闭。关闭时：

- 前端隐藏「日程表」页签、监控名单的「日程」列、进程页的日程进程组，并拒绝直接跳到 `#weekly`；
- 守护进程 `scripts/supervisor.py` 不拉起 `schedule_worker.py`；已在跑的话会写停止文件收起它；
- 四个任务（`weekly_scan` / `reserve` / `weekly_agent` / `daily_push`）同时写进 `meta['tasks_off']`，重新开启时只清这四个，不动用户暂停的其他循环；
- 进程组自己每 5 秒查一次开关，关掉就退出，不依赖管理台常驻。

入口：在「监控名单」的添加框输入 `weekly_open` 点「添加」（大小写不敏感），或调用 `POST /api/weekly/feature`
传 `{"enabled":true}`；`weekly_close` / `{"enabled":false}` 关闭。关键字不会当成 UID 或主播名去查。

每日推送时间默认 08:10，存在 `meta['daily_push_at']`，优先于 `BBOT_PUSH_AT`。日程表页顶部有
「每日推送」卡片（`#wkPushAt` + `#wkPushSave`），保存后推送循环 60 秒内读到，不用重启日程进程组；
`GET /api/weekly/feature` 会多带 `push_at` / `push_at_default`。

### 24.1 路由

| 路由 | 入参 | 说明 |
|---|---|---|
| `GET /api/weekly/feature` | — | `{ok, enabled, tasks, push_at, push_at_default, note}`；默认 `enabled:false`、`push_at:"08:10"` |
| `POST /api/weekly/feature` | `{enabled}` 和/或 `{push_at}` | 开/关总开关，同步四个任务的暂停标记；`push_at` 只改每日推送时间（H:mm，1 分钟内生效）；两个字段至少给一个；PATCH 等价 |
| `GET /api/weekly/agent-api` | — | 返回接入设置和识别状态，凭据只返回是否已保存 |
| `POST /api/weekly/agent-api` | `{mode?, url?, token?, deepseek_key?}` | 保存接入设置。省略凭据字段保留原值，传空字符串清除 |
| `POST /api/weekly/agent-api/test` | `{}` | 使用已保存的地址发送合成测试动态并校验响应 |
| `POST /api/weekly/agent-api/run` | `{}` | 给日程进程组排队，下一轮处理一批候选 |
| `GET /api/weekly/agent` | — | `enabled/limit/slots/pending/last_run/last_result/request_pending/next_run` + `harness{tasks,results,done,task_dir,result_dir,...}` + `agent{last_run,last_items,last_error}` |
| `PATCH /api/weekly/ai` | `{enabled?, limit?, slots?}` | 改闸门开关、每轮条数、时段；空字段 = 不改 |
| `POST /api/weekly/ai` | 同上 | 与 PATCH 等价 |
| `POST /api/weekly/ai/run` | — | 人工排队：写 `weekly_ai_request_at`，日程进程组下一轮立即导出一次 |
| `POST /api/weekly/agent/export` | `{limit?}` | 立即导出任务包，返回 `task_path/result_path/batch_id/items` |
| `POST /api/weekly/agent/import` | — | 回收 `out/ai_results` 并入库，返回回收/入库/复核计数 |

失败一律走 `ApiError` + `hint`（「没到时段 / 没有待确认候选 / 任务文件写不进去」这类可操作提示），前端 `toast()` 直接显示。

### 24.2 日程表入口

展开「日程识别」，选择识别方式，填写所选方式的配置。DeepSeek 直连只需填写 API Key。
保存后可测试接入和排队识别。按钮下方显示操作进度、结果或失败原因。状态区显示待确认数量、排队状态和最近识别结果。

在「每日识别时段」用时间控件选择时间。每天要识别多次时点「添加时段」，选好后点击「保存时段」。
时段按监控台所在电脑的本地时间执行。保存后采用下一次尚未到达的时段，移除全部时段并保存可暂停定时识别，手动排队仍可用。
页面通过 `GET /api/weekly/ai` 读取时段，以 `POST /api/weekly/ai` 保存 `slots` 数组。

### 24.3 交接目录与旧的「导出批次」

任务包写 `out/ai_tasks/<batch_id>.json`，结果读 `out/ai_results/<batch_id>.json`，成功后双方都归档到 `done/`；
契约与判读规则在 `core/weekly/harness.py` 的 `instructions` 里。原来 `scripts/weekly_ai.py run` 往
`data/schedules/inbox/scan_auto.json` 导出的旧路径已随 `batch.py` 的 `batch_file()` 一起删除；
`weekly_ai.py` 现在只剩 gate/status/show/request/set/done（闸门与人工复核）。

### 24.5 回收与入库

日程进程组每轮先回收 Harness 结果，再按时段或手动请求提交下一批。
两种方式共用日程入库、二次判读和人工复核规则。

## 25. 改了代码之后怎么生效（2026-11-11 更新）

- **前端（`web/index.html`）**：服务端按 mtime 读文件，改完直接刷新浏览器即可，不用重启。
  之前页面在 `make_server()` 里读一次就缓存，改了 HTML 必须重启才看得到。
  ⚠️ 若用保留时间戳的工具改文件（例如某些批量编辑/同步工具会保留原 mtime），缓存不会失效——
  刷新还是老页面，`touch web/index.html` 或重启一次即可；判断方法：`curl -s http://127.0.0.1:8765/ | grep`
  一下你刚加的那个字符串，没命中就是缓存没换。
- **后端（`*.py`）**：Python 模块在进程启动时导入，改完要重启进程。0.3.3 起是三个进程
  （监控台主进程 `run_console.py`、采集 `run_bot.py --collector`、推送 `run_pusher.py`，见 §26），按改动落点选：
  - 改了 `web/`（页面与接口）→ 重启监控台主进程；
  - 改了 `core/`、`collector/`（采集侧）→ 重启采集进程；
  - 改了 `notifications/`、`channels/`（推送侧）→ 重启推送进程。
  守护托管时最省事的是整组重启：管理台「服务」页的「重启服务」按钮（`POST /api/service/restart`），
  或写一个空文件 `data\restart.request`；守护会让主进程先收尾（停采集 / 推送、关 QQ 收件、落库、写统计），1 秒后重新拉起，
  这次重启不计入崩溃次数，`data/restart.request` 也会被自动删掉。
  只想动单个后台进程时，用「进程」页对应卡片上的重启按钮（走 `POST /api/stack/action`）。
- **怎么确认生效了**：`data/supervisor.json` 的 `child_pid` 变了，`logs/collector-<日期>.log` /
  `logs/pusher-<日期>.log` 末尾有新的启动行，监控台主进程写 `out/console.log`；页面里新加的 tab / 卡片随之出现。

## 26. 三进程与「接入指引」（2026-11-11）

三进程拆分之后，这一页看到的「一个进程」其实分成了三个，各自负责一件事：

| 进程 | 启动者 | 负责 | 默认 |
| --- | --- | --- | --- |
| 监控台主进程 `scripts/run_console.py` | 守护 / 自启 / `install.ps1` 第 5 步 | 服务页面（127.0.0.1:8765）、写 `data/console.beat` 心跳、托管下面两个进程 | 随安装启动 |
| 采集进程 `scripts/run_bot.py --collector` | 监控台主进程 | 直播 + 动态取数入库，不派发、不清缓存、不起页面 | **关**；绑上 B 站账号后自动开 |
| 推送进程 `scripts/run_pusher.py` | 监控台主进程 | 从持久队列派发 QQ、收私聊指令、发清理通知、缓存清理 | **关**；绑上 QQ 机器人后自动开 |

为什么要拆：以前采集在同一个 asyncio 循环里顺手派发，B 站慢或被风控时，QQ 发送和页面都会被一起拖住。现在页面只靠自己那一个进程，队列在库里，采集或推送崩了、重启了都不丢消息。

### 26.1 谁决定「起不起」

- 口径只有一个：`shared/readiness.py`。
  - 采集进程需要「B 站已绑定」：`config/bili.env` 里有非空、非占位、长度够的 `SESSDATA`；
  - 推送进程需要「QQ 已绑定」：`BBOT.env` 有 AppID / AppSecret，且 `.p0_ids.json` 有 `user_openid`。
- 监控台里的 `StackManager`（`web/stack.py`）每 2 秒对账一次：该起就起、条件没了就写停止文件让它优雅退出；异常退出按 5 → 300 秒退避重启，连续崩到阈值后停下来并在 `data/stack.json` 里留 `last_error`。
- 状态落在 `data/stack.json`（原子写），日志分别是 `logs/collector.log`、`logs/pusher.log`（按天落盘成 `collector-2026-11-11.log` 这种名字）。

### 26.2 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/stack` | 绑定状态（B 站 / QQ，含掩码后的 sessdata、app_id、openid）+ 两个角色是否在跑、pid、重启次数、上次退出码与错误；没托管能力时 `available: false` 并给 `hint` |
| POST | `/api/stack/action` | body `{role, action}`；`role` ∈ `collector` / `pusher`，`action` ∈ `start` / `stop` / `restart` / `auto`（auto = 回到「跟着绑定状态」）。未绑定时启动会被拒（409）并说明去哪绑 |

### 26.3 进程页的变化

- 多出「采集进程」「推送进程」两行，可启动 / 重启 / 停止；行里显示 pid、重启次数、上次退出码、上次错误。
- 6 个循环行前面标了归属：`live`、`dynamic` 归采集进程，`dispatch`、`cache_gc`、`qq_inbox`、`cleanup_notify` 归推送进程（`core/tasks.py` 的 `OWNER`），判活也按归属的进程算，不再一律看主循环心跳。
- 某个循环显示「未运行」时，先看它归属的进程是不是没起（多半是还没绑账号）。
- 这两行也带「开机自启」勾选框（语义是「随监控台启动」而不是计划任务，见 §22.11）：采集 / 推送默认开，弹幕监控台与录播清理守护默认关，改完下次启动监控台生效。

### 26.4 首次接入指引

- 顶栏的「接入指引」按钮常驻，绑好后左边圆点由黄变绿；首次打开且没绑全时自动弹出（`localStorage` 键 `bbot_guide_seen`），关掉后不再自动打扰，按钮可随时重看。
- 四步按真实状态打勾，每步都能一键跳到该去的地方：
  1. 绑定 B 站账号 —— 「去扫码登录」直接开概览页的二维码；
  2. 配置 QQ 机器人 —— 「去填凭据」跳到机器人页的 AppID 输入框，「抓推送目标」跳到抓取按钮；
  3. 导入监控名单 —— 跳到「监控名单」页；
  4. 确认采集 / 推送进程 —— 显示两个进程的状态，可单独「重启采集 / 重启推送」。
- 指引读的就是 `GET /api/stack`，所以绑定状态一变，勾选和圆点立刻跟着变。

### 26.5 取舍

- **BOARD 由 ConsoleCore 自己绑库**（2026-11-11）：进程页开关落库在 `meta['tasks_off']`，写之前必须先知道用哪个库。`Board` 是进程级单例，以前只有 `run_bot.py` 那条老链路会 bind，换成 `run_console.py` 之后点开关直接 500「没有绑定数据库」。现在 `ConsoleCore.__init__` 统一 `T.BOARD.bind(self.db_path)`，`scripts/run_console.py` 再显式绑一次；新增启动入口时不必再记着绑库，`tests/test_console_board.py` 会盯住这行。
- **不绑定就不起进程**：装完零配置也能打开页面，但什么都不会发；这比「装完先报一堆缺凭据的错误」更适合交接。
- **进程崩溃不弹窗**：只记在 `data/stack.json` 和进程页，避免交接时被无关弹窗劝退。
- **保留 `run_bot.py --web` 的旧跑法**：调试时仍可单进程全包，但那种管理台没有托管能力，`/api/stack` 会明确说清楚。
