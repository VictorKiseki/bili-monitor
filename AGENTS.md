# 项目底层要求（AGENTS.md）

本文件是这个仓库的**硬性约定**，优先级高于任何单次任务说明、口头约定和临时习惯。
不论是人工提交还是 AI Agent 代改，只要改动进入本仓库，就必须满足这里的每一条。

可以自动检查的部分已经接进 `scripts/release.py check` 与 `tests/`（见 §8），
其余靠 review 把关——**不知道能不能做时，先读这里，不要凭直觉**。

---

## 0. 适用范围与基本前提

- **目标平台**：Windows 10 1809+ / Windows 11；Python **3.10 – 3.12（64 位）**。
  不引入只有 Linux/macOS 才能跑的实现；跨平台写法可以，但必须在 Windows 上验证过。
- **发布形态是绿色包**：`runtime/` 里放 CPython 官方 embeddable（默认 3.12.7 x64）+ 预装依赖，
  用户机器**不装 Python、不联网**也要能跑。`runtime/` 由 `python scripts/build_runtime.py` 生成、
  不入库，但**必须**随 zip 分发；`release.py check` 会查 `python312.zip` 等关键文件是否齐全。
- **两条分支线**（详见 [docs/34-开源发布清单.md](docs/34-开源发布清单.md) §9）：
  - `master` = 开发/测试线，日常提交都在这条线上；
  - `opensource` = 发布线，只收「已确认」的整批迭代，一次发版一条快照提交。
- **运行数据与真实凭据永不入库**：`BBOT.env`、`config/*.env`、`.p0_ids.json`、`config/*.json`
  （真实名单）、`data/`、`logs/`、`out/`、`dist/`、`.venv/`、`runtime/` 全部在 `.gitignore` 里。
- **单一事实来源**：版本号只有 `VERSION`，依赖只有 `requirements.txt`，
  关键行为契约只有 `shared/contracts/`。不要在任何别处复制一份再改。
- **进程只有三个**（详见 [docs/12](docs/12-Web管理台.md) §26）：监控台主进程 `scripts/run_console.py`
  只服务页面并托管后台；采集进程 `scripts/run_bot.py --collector` 只入库（`dispatcher=None`）；
  推送进程 `scripts/run_pusher.py` 只派发。**不要**把派发写回采集循环，也不要让页面依赖采集心跳。
  起停条件只有一个口径：`shared/readiness.py`（B 站绑定 → 采集，QQ 绑定 → 推送）。

---

## 1. 版本迭代规则（不可协商）

从 `0.1.0` 起，版本号形如 `主.次.修`，三段各有明确触发条件。

| 位置 | 谁能改 | 什么时候递增 | 例子 |
|---|---|---|---|
| 第一位 `major` | **只能人工** | 影响整个项目架构的调整，或新增功能模块 | 换存储层、加独立子产品：`0.9.4` → `1.0.0` |
| 第二位 `minor` | 自动 | 多个重要功能统一合并迭代 | Harness 交接 + 关注流替换：`0.1.1` → `0.2.0` |
| 第三位 `patch` | 自动 | 缺陷修复、细节优化，以及尚未合并的小改动 | 页面不刷新、下拉不落盘：`0.2.0` → `0.2.1` |

**R1 第一位只能人工加。** 脚本不传 `--allow-major` 会直接拒绝（退出码 2）。
自动化流程、CI、顺手改版本号的提交一律不允许动第一位；确需动，由人写入提交说明并显式放行。

**R2 第二位、第三位是自动的。** 不需要"请示"，但必须是这一版真实发生的事，
不能为了凑数字把 patch 改成 minor。

**R3 低位归零。** 涨哪一位，它右边的位全部清零：`0.1.1` 升第二位得 `0.2.0`（不是 `0.2.1`），
`0.9.4` 升第一位得 `1.0.0`；只有 `patch` 在原位加一。`shared/version.py` 的 `bump()`
已如此实现，`tests/test_version.py` 有断言守着。

**R4 每个版本必须有文字记录。** `CHANGELOG.md` 里这一版要有一节，写清
**新增 / 变更 / 修复 / 安全 / 升级提示**（没有的类别可以省略，但不能整节只写"若干修复"）。
**禁止只改 `VERSION` 不写记录**——那种提交会被 review 退回。

**R5 版本号唯一来源是 `VERSION`。** 代码、脚本、文档、页面一律通过
`shared/version.py`（`read_version()` / `__version__`）读，不允许再出现写死的版本号
（文档里的历史版本示例除外，且必须标明是示例）。

**R6 tag 与版本号一致。** 发版打 annotated tag `v<version>`（例如 `v0.2.0`）；
个人线另打 `v<version>-personal`。删改已发布的 tag 视为破坏性操作。

**R7 多条同时命中时取最高位。** 一次迭代里既有新功能模块、又有多个功能合并、还有修复，
按 `major > minor > patch` 取一处，不要拆成三个号。

命令（在仓库根目录）：

```powershell
.\.venv\Scripts\python.exe scripts\release.py bump patch            # 修复 / 细节
.\.venv\Scripts\python.exe scripts\release.py bump minor            # 多个重要功能合并
.\.venv\Scripts\python.exe scripts\release.py bump major --allow-major   # 架构级 / 新功能模块（人工）
```

`bump` 会改 `VERSION` 并在 `CHANGELOG.md` 插入带日期的新小节骨架；
**然后必须把内容补全**，再 `check` + `build`。

详细流程与发版四步见 [docs/31-发布与交接手册.md](docs/31-发布与交接手册.md) §11。

---

## 2. 提交前必跑（本地绿了再提交）

```powershell
# 自带运行时优先；源码仓库没生成 runtime/ 时用 .venv（路径换成 .\.venv\Scripts\python.exe）
.\runtime\python.exe scripts/run_tests.py   # 全量离线用例，1 例按环境跳过
.\runtime\python.exe scripts\release.py check                       # 必须输出「干净，可以 build」
```

改了 `web/index.html` 的内联 JS，再抽出来过一遍 `node --check`（做法见
[`.github/workflows/ci.yml`](.github/workflows/ci.yml)）。

**CI 只做同样的检查，不是额外保险**：本地不跑的检查，不能指望 CI 兜住。

---

## 3. 提交与分支

- 提交信息：`type(scope): 一句话说清做了什么`，正文写**为什么**这么改（`feat`/`fix`/`docs`/`test`/`refactor`/`chore`/`perf`）。
- 业务改动只提交到 `master`；`opensource` 只接受「整批确认后的快照」，按树同步（`git read-tree master^{tree}` → 剔除 `tests/` 与 `scripts/selftest_*.py` → `commit-tree`），不走 merge。流程见 [docs/34](docs/34-开源发布清单.md) §9。
  `.github/tests/` 的公开 CI 用例只用合成数据和公开模板，随快照同步。
- 一个 PR 做一件事；顺手发现的问题单独开 PR。
- 协作细节（环境准备、PR 模板、review 约定）见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 4. 凭据与隐私

- **真实值只写在本机文件**：`BBOT.env`、`config/bili.env`、`config/livedeck.env`、
  `config/bili_follow.env`、`.p0_ids.json`；模板一律 `*.example` + 占位值。
- 管理台只监听 `127.0.0.1`，不要为了"方便"改成 `0.0.0.0`。
- 日志与数据库里不写 SESSDATA、AppSecret、`user_openid` 明文；报错信息给提示，不给原文。
- 新增任何配置项：先加 `*.example`，再在 `scripts/preflight.py` 里加检查，最后写进文档。

---

## 5. 编码硬约束

- **`requirements.txt` 必须是纯 ASCII**：pip 在中文 Windows 上按系统本地编码读它，中文注释直接抛
  `UnicodeDecodeError`；说明文字写进行尾英文注释或文档。
- **PowerShell 脚本带 UTF-8 BOM、CRLF**：PS 5.1 按 ANSI 读无 BOM 文件时，中文尾字节会吞掉紧随的 `{`/`}`。
  新增 `.ps1` 必须补 BOM（`release.py check` 会查）。
- **`runtime/` 由脚本生成，不手改**：重建一律 `python scripts/build_runtime.py --force`。
  `python312._pth` 的四行内容（`python312.zip` / `.` / `Lib\site-packages` / 空行 / `import site`）
  与 `runtime/LICENSE.txt` 都是硬要求；`#import site` 这种注释掉的状态会被 `build_runtime.py --check`
  与 `release.py check` 同时判死。运行时里的 `.pyc`/`__pycache__` 不进包。
- **`web/index.html` 是单文件 ES5**：没有构建步骤，别用可选链 / `async`/`await` 之外的新语法糖；
  请求统一走 `api(path, opt)`，提示统一走 `toast(msg, err)`。
- **新路由同步三处**：`web/console_*.py`（数据层）→ `web/console_http.py`（分派）→ `docs/12-Web管理台.md`（路由表）。
- **不新增运行时依赖**，除非确有必要：固定版本、写进 `requirements.txt`、在 `CHANGELOG` 的「变更」里说明理由。
- **网络调用可注入**：新代码接受 client 参数，测试用假 client，不打死接口。
- **注释与文档中文、标识符英文**：沿用现有风格。
- **测试只依赖标准库** `unittest`，不引入 pytest；离线可跑。

---

## 6. 行为变更的同步清单

改一处功能，通常要连带改这些（缺一即算没做完）：

| 改动类型 | 必须同步 |
|---|---|
| 新功能 / 行为变化 | 代码 + 离线测试 + `CHANGELOG`（`[Unreleased]` 或当前小节） |
| 新增配置项 | `*.example` + `scripts/preflight.py` 检查 + `docs/31` 或对应专题文档 |
| 新路由 / 页面 | `console_*.py` + `console_http.py` + `docs/12` + `web/index.html` |
| 进程结构 / 启动方式 | `scripts/run_console.py`、`scripts/run_pusher.py`、`scripts/run_bot.py`（`--collector`）、`web/stack.py`、`shared/readiness.py`、`scripts/service.ps1`、`scripts/service_start.cmd`、`scripts/supervisor.py`（心跳）、`scripts/install.ps1`、`core/tasks.py` `OWNER`、`docs/12` §26、`docs/31` §6、`tests/test_stack.py` / `test_pusher.py` / `test_collector_mode.py` |
| 安装 / 发版流程 | `scripts/install.ps1`、`scripts/release.py`、`scripts/release_gitee.py`、`docs/31`、`docs/34`、`AGENTS.md`（本文件） |
| 运行时 / 绿色包 | `scripts/build_runtime.py`、`scripts/release.py`（`should_pack`/`check_runtime`）、`scripts/preflight.py`、启动脚本（runtime 优先）、`docs/13` §10.4.1、`docs/31` §1–2、`docs/34` §6 |
| 版本号规则 | 本文件 §1 + `shared/version.py` `VERSION_RULES` + `docs/31` §11 + `CHANGELOG` 抬头 + 测试 |

---

## 7. 明确不做的事

- 不做商业运营、付费分发、二次售卖（见 `LICENSE`）。
- 不加绕过 B 站/QQ 风控的手段（高频轮询、多账号轮换刷量、伪装设备指纹）。
- 不把项目改成需要注册中心/外部数据库才能跑；保持「解压即用、本地单机」。
- 不在没有测试的情况下重构核心采集与推送路径。

---

## 8. 这些要求怎么被检查

| 规则 | 检查者 |
|---|---|
| 关键文件齐全（含本文件、`VERSION`、`CHANGELOG`、`LICENSE`） | `scripts/release.py check` |
| 没有凭据混进发布包 | `scripts/release.py check`（正则扫描 200+ 文件） |
| PowerShell 脚本都带 BOM | `scripts/release.py check` |
| 自带运行时齐全（python312.zip / `_pth` / site-packages / PSF 许可） | `scripts/build_runtime.py --check` + `scripts/release.py check` |
| `runtime/` 整目录进包、`.pyc` 不进包 | `tests/test_runtime_bundle.py` |
| 解释器判定为「自带运行时」而非「系统 Python」 | `tests/test_runtime_bundle.py` + `preflight.py` |
| `VERSION` 是合法语义化版本、`CHANGELOG` 有当前版本小节 | `tests/test_version.py` |
| 第一位必须人工放行、低位归零 | `tests/test_version.py` |
| 本文件与 `CHANGELOG` 都写明了三位规则 | `tests/test_version.py` |
| 其余（§3–§7） | review |

有规则被证明不合理时：**先改本文件与对应检查，再改代码**——不要"代码先松，文档保持严格"。

---

## 9. 相关文档

| 想知道 | 去哪 |
|---|---|
| 怎么装、怎么接账号、怎么发版 | [docs/31-发布与交接手册.md](docs/31-发布与交接手册.md) |
| 协作流程与 PR | [CONTRIBUTING.md](CONTRIBUTING.md) |
| 两条分支怎么维护 | [docs/34-开源发布清单.md](docs/34-开源发布清单.md) §9 |
| 文档总索引与代码地图 | [docs/37-文档与代码索引.md](docs/37-文档与代码索引.md)（README 只留入口） |
