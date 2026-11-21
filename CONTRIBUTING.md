# 参与贡献

感谢你愿意花时间改进这个项目。下面这些约定是为了让改动好 review、好回滚、不给使用者添麻烦。

> **动手前先读 [AGENTS.md](AGENTS.md)**：那是本项目的底层要求（版本迭代规则、必跑检查、凭据与编码硬约束），
> 本文件是它的操作版；两者冲突时以 `AGENTS.md` 为准。

## 1. 环境准备

项目目标平台是 **Windows 10 1809+ / Windows 11（64 位）**。

发布包自带 Python 运行时（`runtime\`），但**源码仓库里没有它**（`.gitignore` 排除，由构建生成）。
所以第一次拉下代码后，先准备一个能跑的解释器，二选一：

```powershell
git clone https://gitee.com/victorkiseki/bili-monitor.git bili-monitor
cd bili-monitor

# 方式 A（推荐，和发布包完全一致）：用 3.12 x64 的解释器生成自带运行时
python scripts\build_runtime.py            # 下载 CPython embeddable + 预装依赖，约 45 MB
.\runtime\python.exe scripts\preflight.py

# 方式 B：自己建虚拟环境（Python 3.10 – 3.12 64 位）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\preflight.py
```

> 两种方式都能跑测试。后续命令里的 `.\runtime\python.exe` 换成 `.\.venv\Scripts\python.exe` 即可。
> 生成 `runtime\` 需要联网（下载 CPython 官方 embeddable 与 wheel，仅构建机需要）。

> 提交代码前**不需要**真的接上 QQ 机器人或登录 B 站：单元测试全部离线（网络调用用 `unittest.mock` 替身）。

## 2. 提交前必跑的三件事

```powershell
# 1) 单元测试（全量离线用例，其中 1 个按环境跳过）
.\runtime\python.exe scripts/run_tests.py

# 2) 发布前检查：关键文件齐全、运行时完整、没有夹带凭据、PowerShell 脚本编码正确
.\runtime\python.exe scripts\release.py check

# 3) 只改了前端时，顺手校验内联 JS 语法
node --check <(把 web/index.html 里的 <script> 抽出来)   # 见 .github/workflows/ci.yml 的做法
```

CI 会跑三层：Windows + Python 3.10 / 3.11 / 3.12 的单元测试与 `release.py check`；
`build_runtime.py` 现建运行时后打绿色包并解压冒烟；外加内联 JS 语法与文档内链检查。本地绿了 CI 基本就绿。

## 3. 版本迭代（硬性要求，详见 AGENTS.md §1）

| 位置 | 谁能改 | 触发条件 | 例子 |
|---|---|---|---|
| 第一位 `major` | **只能人工**（`--allow-major`） | 架构级调整或新增功能模块 | `0.9.4` → `1.0.0` |
| 第二位 `minor` | 自动 | 多个重要功能统一合并迭代 | `0.1.1` → `0.2.0` |
| 第三位 `patch` | 自动 | 修复、细节优化、尚未合并的小改动 | `0.2.0` → `0.2.1` |

三条不能忘：**涨位时低位归零**（`0.1.1` 升第二位是 `0.2.0`，不是 `0.2.1`）；
**每个版本都要在 `CHANGELOG.md` 留一节文字记录**；**版本号只从 `VERSION` 读**。
日常提 PR 不自己改 `VERSION`，由维护者在发版时 `release.py bump`。

## 4. 代码约定

- **不引入新的运行时依赖**，除非确实必要：装好、固定版本、写进 `requirements.txt`（纯 ASCII 注释！pip 在中文 Windows 上按系统本地编码读这个文件，中文注释会直接抛 `UnicodeDecodeError`），并在 `CHANGELOG.md` 的「变更」里说明。
- **网络与文件系统要有替身**：新增的 HTTP 调用请接受可注入的 client，测试里用假 client；文件交接类逻辑参照 `tests/test_harness.py` 用临时目录，不要打真接口、不要动真实 `out/`。
- **凭据永不入库**：真实值只写在 `BBOT.env` / `config/*.env` / `.p0_ids.json`（均在 `.gitignore`），模板一律 `*.example` + 占位值。
- **注释与文档中文、标识符英文**：现有代码都是这个风格。
- **前端是单文件 ES5**：`web/index.html` 里没有构建步骤，别用可选链、`async/await`、模板字符串以外的现代语法糖；新接口沿用 `api(path, opt)` + `toast(msg, err)`。
- **改路由要同步三处**：`web/console_*.py`（数据层）→ `web/console_http.py`（分派）→ `docs/12-Web管理台.md`（路由表）。

## 5. 提交信息

`type(scope): 一句话说清做了什么`，正文说明**为什么**这么改。

| type | 用途 |
|---|---|
| `feat` | 新功能 |
| `fix` | 修 bug |
| `docs` | 只改文档 |
| `test` | 只改测试 |
| `refactor` | 不改行为的重构 |
| `chore` | 依赖、脚本、打包、格式 |
| `perf` | 性能 |

示例：`fix(install): PowerShell 5.1 下 python 版本探针的引号被剥掉`

## 6. 分支与 PR

1. 从 `opensource` 切分支：`feat/xxx`、`fix/xxx`、`docs/xxx`。
2. 一个 PR 只做一件事；顺手修别的请单独开 PR。
3. PR 描述里写清：**改了什么 / 为什么 / 怎么验证**（命令与关键输出）。
4. CI 绿了再请求 review；review 意见用新 commit 回应（不要 force push 到已 review 的分支）。
5. 合并后由维护者更新 `CHANGELOG.md` 的 `[Unreleased]`，发版时再 `bump`（版本号规则见 §3）。

## 7. 提交前自查清单

- [ ] `python scripts/run_tests.py` 全绿
- [ ] `python scripts/release.py check` 输出「干净，可以 build」
- [ ] 新增/修改的行为有对应用例（离线）
- [ ] 新路由 / 新配置项已写进 `docs/12` 或 `docs/31`
- [ ] `CHANGELOG.md` 的 `[Unreleased]` 有对应条目（发版小节按 AGENTS.md §1 的规则写全）
- [ ] 没有把 `BBOT.env`、`config/*.env`、`.p0_ids.json`、`data/`、`logs/` 提交进去
- [ ] 改了 PowerShell 脚本的话，文件仍是 **UTF-8 with BOM**（否则 PowerShell 5.1 会把中文尾字节当成语法）

## 8. 想改但不知道动哪

| 目标 | 入口 |
|---|---|
| 抓取 / 登录 / WBI 签名 | `collector/` |
| 事件判定与主循环 | `core/watcher.py`、`core/tasks.py` |
| 卡片图与渲染 | `notifications/` |
| QQ 官方机器人发送 / 绑定 | `channels/official_qq.py`、`channels/qq_setup.py` |
| 日程（周表）识别 | `core/weekly/`（`parse/fetch/store/agent/batch`） |
| 日程识别交接（任务包/结果） | `core/weekly/harness.py`、`scripts/weekly_agent.py` |
| 管理台后端 / 前端 | `web/console_*.py`、`web/index.html` |
| 安装、自检、发版 | `scripts/install.ps1`、`scripts/preflight.py`、`scripts/release.py` |

代码地图的完整版在 [docs/37-文档与代码索引.md](docs/37-文档与代码索引.md)。

## 9. 行为准则与安全问题

- 参与讨论请遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
- 发现安全问题时**不要**开公开 issue，见 [SECURITY.md](SECURITY.md)。
