"""环境自检（preflight）：交接/安装/排障时第一条要跑的命令。

它只读不写：不建库、不改配置、不联网、不启动进程。

用法：
  python scripts/preflight.py            # 人读报告，全部通过退出码 0
  python scripts/preflight.py --json     # 机器可读，给安装脚本 / CI 用
  python scripts/preflight.py --strict   # 警告也算失败（发布前验收用）
  python scripts/preflight.py --port 8770   # 换一个端口检查

退出码：
  0 = 没有阻塞问题（可能仍有 ⚠ 警告）
  1 = 有阻塞问题（✗）
  2 = 参数错误或自身异常
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # 允许直接 `python scripts/preflight.py` 运行
    sys.path.insert(0, str(ROOT))

#: 与 scripts/run_bot.py 的 REQUIRED_DEPENDENCIES 保持一致（pip 名 / import 名 / 用途）
REQUIRED_DEPENDENCIES = [
    ("httpx", "httpx", "HTTP 请求"),
    ("Pillow", "PIL", "卡片图渲染"),
    ("websockets", "websockets", "QQ 官方 WS 收事件"),
    ("websocket-client", "websocket", "WebSocket 兜底"),
    ("segno", "segno", "登录二维码"),
]

OPTIONAL_DEPENDENCIES = [
    ("brotli", "brotli", "br 压缩"),
    ("psutil", "psutil", "进程信息"),
]

#: 配置模板里的占位值特征；命中任意一个就认为「还没填」
PLACEHOLDER_HINTS = ("填", "YOUR", "your_", "xxxx", "XXXX", "<", "替换", "示例", "example", "TODO", "todo")

OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: "[OK]  ", WARN: "[WARN]", FAIL: "[FAIL]"}


@dataclass
class Check:
    key: str
    level: str
    title: str
    detail: str = ""
    hint: str = ""

    def as_dict(self) -> dict:
        return {"key": self.key, "level": self.level, "title": self.title,
                "detail": self.detail, "hint": self.hint}


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    version: str = ""
    root: str = ""

    def add(self, key: str, level: str, title: str, detail: str = "", hint: str = "") -> Check:
        c = Check(key, level, title, detail, hint)
        self.checks.append(c)
        return c

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.level == FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.level == WARN]

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "root": self.root,
            "ok": not self.failures,
            "summary": {"ok": sum(c.level == OK for c in self.checks),
                        "warn": len(self.warnings), "fail": len(self.failures)},
            "checks": [c.as_dict() for c in self.checks],
        }


# ---------------------------------------------------------------- 工具

def _read_kv(path: Path) -> dict[str, str]:
    """读 KEY=VALUE 形式的 env 文件，忽略注释/空行。"""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _is_placeholder(value: str) -> bool:
    if not value.strip():
        return True
    return any(h in value for h in PLACEHOLDER_HINTS)


def _can_write(directory: Path) -> tuple[bool, str]:
    """真写一个临时文件，最可靠地判断目录可写（不只看权限位）。"""
    probe = directory / f".preflight-{os.getpid()}.tmp"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, str(directory)
    except OSError as exc:
        return False, f"{directory}：{exc}"
    finally:
        try:
            if probe.exists():
                probe.unlink()
        except OSError:
            pass


def _port_free(host: str, port: int) -> tuple[bool, str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.5)
        try:
            s.bind((host, port))
        except OSError as exc:
            return False, f"{host}:{port} 已被占用（{exc.strerror or exc}）"
    return True, f"{host}:{port} 空闲"


def _db_schema(db: Path) -> tuple[int | None, str]:
    """只读地读 meta.schema_version；库里什么表都没有时返回 (0, '空库')。"""
    try:
        uri = f"file:{db.as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        return None, f"打不开：{exc}"
    try:
        row = con.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        return (int(row[0]) if row else 0), ""
    except sqlite3.Error as exc:
        return 0, f"读不到 meta 表：{exc}"
    finally:
        con.close()


def _code_schema_version() -> int:
    try:
        from store.sqlite import SCHEMA_VERSION  # 延迟导入：缺依赖时也不至于整个自检崩掉
        return int(SCHEMA_VERSION)
    except Exception:
        return 0


# ---------------------------------------------------------------- 各项检查

def python_kind(root: Path | None = None, exe: str | Path | None = None) -> str:
    """判断解释器是什么形态：runtime / venv / system。

    「自带运行时」（runtime/）和「虚拟环境」都算隔离环境，只有 system 才提醒。
    这个函数给测试用，所以不打印、不抛异常；exe 参数只是为了离线测试能注入假路径。
    """
    root = Path(root or ROOT)
    try:
        exe_p = Path(exe or sys.executable).resolve()
        rt = (root / "runtime").resolve()
        if rt in exe_p.parents:
            return "runtime"
    except OSError:
        pass
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        return "venv"
    return "system"


def check_python(report: Report, root: Path | None = None) -> None:
    info = sys.version_info
    ver = f"{info.major}.{info.minor}.{info.micro}"
    if info < (3, 10):
        report.add("python.version", FAIL, "Python 版本", f"{ver}（需要 3.10+）",
                   "装 Python 3.11/3.12 后重新运行 install.cmd")
    else:
        report.add("python.version", OK, "Python 版本", f"{ver}")
    report.add("python.exe", OK, "解释器路径", sys.executable)
    kind = python_kind(root)
    if kind == "runtime":
        report.add("python.env", OK, "运行环境", f"自带运行时 {sys.prefix}")
    elif kind == "venv":
        report.add("python.env", OK, "运行环境", f"虚拟环境 {sys.prefix}")
    else:
        report.add("python.env", WARN, "运行环境", f"系统全局 Python（{sys.prefix}）",
                   "推荐直接用项目自带的 runtime\\python.exe（发布包已经带好）")


def check_dependencies(report: Report) -> None:
    import importlib
    for pip_name, import_name, purpose in REQUIRED_DEPENDENCIES:
        try:
            mod = importlib.import_module(import_name)
        except Exception as exc:  # ImportError 也可能是 DLL 加载失败
            report.add(f"deps.{import_name}", FAIL, f"依赖 {pip_name}", f"缺失或加载失败：{exc}",
                       "python -m pip install -r requirements.txt")
            continue
        ver = getattr(mod, "__version__", "?")
        report.add(f"deps.{import_name}", OK, f"依赖 {pip_name}", f"{ver}（{purpose}）")
    for pip_name, import_name, purpose in OPTIONAL_DEPENDENCIES:
        try:
            mod = importlib.import_module(import_name)
            report.add(f"deps.{import_name}", OK, f"可选依赖 {pip_name}",
                       f"{getattr(mod, '__version__', '?')}（{purpose}）")
        except Exception:
            report.add(f"deps.{import_name}", WARN, f"可选依赖 {pip_name}", "未安装（不影响主流程）",
                       "需要的话：python -m pip install -r requirements-optional.txt")


def check_configs(report: Report, root: Path) -> None:
    agent_config = root / "config" / "weekly_agent.json"
    if agent_config.is_file():
        try:
            from core.weekly import agent_api

            config = agent_api.load_config(agent_config)
        except ImportError:
            report.add("config.weekly_agent", WARN, "自定义日程识别 Agent", "安装依赖后可校验配置")
        except ValueError as exc:
            report.add("config.weekly_agent", WARN, "自定义日程识别 Agent", str(exc),
                       "在日程表的「日程识别」中重新保存设置")
        else:
            mode = {"http": "自定义 Agent API", "harness": "Harness 文件交接",
                    "deepseek": "DeepSeek 直连"}[config["mode"]]
            if config["mode"] == "deepseek" and not config["deepseek_key"]:
                report.add("config.weekly_agent", WARN, "日程识别 Agent", "请填写 DeepSeek API Key")
            else:
                report.add("config.weekly_agent", OK, "日程识别 Agent", mode)

    # --- QQ 官方机器人凭据
    bbot = root / "BBOT.env"
    if not bbot.is_file():
        report.add("config.bbot", WARN, "QQ 机器人凭据 BBOT.env", "未配置（全新安装的正常状态）",
                   "打开监控台按「接入指引」→ 机器人页填 AppID / AppSecret"
                   "（命令行等价：python scripts/setup_wizard.py --qq）")
    else:
        env = _read_kv(bbot)
        app_id = env.get("QQBOT_APP_ID", "")
        secret = env.get("QQBOT_APP_SECRET", "")
        if _is_placeholder(app_id) or _is_placeholder(secret):
            report.add("config.bbot", FAIL, "QQ 机器人凭据 BBOT.env", "还是占位值，没有填真凭据",
                       "到 q.qq.com 机器人后台拿 AppID/AppSecret，跑 python scripts/setup_wizard.py 写入")
        else:
            report.add("config.bbot", OK, "QQ 机器人凭据 BBOT.env",
                       f"AppID {app_id[:4]}****{app_id[-2:]}")

    # --- 推送目标 openid
    ids = root / ".p0_ids.json"
    if not ids.is_file():
        report.add("config.p0ids", WARN, "推送目标 .p0_ids.json", "未配置（全新安装的正常状态）",
                   "先私聊机器人一句话，再到监控台「机器人」页抓推送目标"
                   "（命令行等价：python scripts/setup_wizard.py --ids）")
    else:
        try:
            openid = json.loads(ids.read_text(encoding="utf-8")).get("user_openid", "")
        except (OSError, ValueError) as exc:
            report.add("config.p0ids", FAIL, "推送目标 .p0_ids.json", f"内容不是合法 JSON：{exc}",
                       "删除后重新用 p0_check.py listen 生成")
        else:
            if _is_placeholder(str(openid)) or len(str(openid)) < 16:
                report.add("config.p0ids", FAIL, "推送目标 .p0_ids.json", "openid 为空或还是占位值",
                           "python scripts/p0_check.py listen，然后在 QQ 里私聊机器人一句话")
            else:
                report.add("config.p0ids", OK, "推送目标 .p0_ids.json",
                           f"user_openid {str(openid)[:6]}****{str(openid)[-4:]}")

    # --- B 站登录态（可选，但强烈建议）
    bili = root / "config" / "bili.env"
    if not bili.is_file():
        report.add("config.bili", WARN, "B 站登录态 config/bili.env", "未配置（匿名模式）",
                   "监控台顶部「接入指引」→ 去扫码登录（命令行等价：python scripts/bili_qr_login.py）；"
                   "匿名模式撞 -352 会更频繁")
    else:
        sess = _read_kv(bili).get("SESSDATA", "")
        if _is_placeholder(sess) or len(sess) < 20:
            report.add("config.bili", WARN, "B 站登录态 config/bili.env", "SESSDATA 为空或占位",
                       "python scripts/bili_qr_login.py 扫码登录")
        else:
            report.add("config.bili", OK, "B 站登录态 config/bili.env", f"SESSDATA {sess[:4]}****{sess[-3:]}")

    # --- 关注流凭据（可选；有它才能一次拿回所有关注的新动态）
    follow = root / "config" / "bili_follow.env"
    if not follow.is_file():
        report.add("config.follow", WARN, "关注流凭据 config/bili_follow.env", "未配置（按逐人轮询取动态）",
                   "想用关注流就扫码：python scripts/follow_feed_login.py，或管理台「B 站登录态」扫码")
    else:
        kv = _read_kv(follow)
        sess = kv.get("SESSDATA", "")
        jct = kv.get("BILI_JCT", "") or kv.get("bili_jct", "")
        if _is_placeholder(sess) or len(sess) < 20:
            report.add("config.follow", WARN, "关注流凭据 config/bili_follow.env", "SESSDATA 为空或占位",
                       "python scripts/follow_feed_login.py 重新扫码")
        elif not jct or len(jct) < 20:
            report.add("config.follow", WARN, "关注流凭据 config/bili_follow.env",
                       "缺 bili_jct（关注同步不可用，动态仍按逐人轮询）",
                       "python scripts/follow_feed_login.py 重新扫码（bili_jct 与 SESSDATA 同生命周期）")
        else:
            report.add("config.follow", OK, "关注流凭据 config/bili_follow.env",
                       f"SESSDATA {sess[:4]}****{sess[-3:]}，bili_jct 已配置")

    # --- 主播名单
    subjects = root / "config" / "subjects.json"
    if not subjects.is_file():
        report.add("config.subjects", WARN, "主播名单 config/subjects.json", "未配置（全新安装的正常状态）",
                   "在监控台「监控名单」页添加；也可以复制 config/subjects.json.example 再改")
    else:
        try:
            data = json.loads(subjects.read_text(encoding="utf-8"))
            subs = data.get("subjects") or []
        except (OSError, ValueError) as exc:
            report.add("config.subjects", FAIL, "主播名单 config/subjects.json", f"解析失败：{exc}",
                       "检查 JSON 语法（多余逗号是最常见原因）")
        else:
            if not subs:
                report.add("config.subjects", FAIL, "主播名单 config/subjects.json", "名单是空的",
                           "至少加一个 uid/room_id，或用 scripts/import_follows.py 导入关注列表")
            else:
                poll = data.get("poll") or {}
                report.add("config.subjects", OK, "主播名单 config/subjects.json",
                           f"{len(subs)} 个主播，取数 {poll.get('mode', 'auto')}，"
                           f"直播轮询 {poll.get('live_interval_s', '默认')}s，"
                           f"动态 {poll.get('feed_interval_s', poll.get('dynamic_interval_s', '默认'))}s")

    # --- 可选：录像清理 / 直播台登录态
    for key, rel, title, hint in (
        ("config.recorder", "config/recorder_cleanup.json", "录像清理配置（可选）",
         "不用录像清理可以不管；要用就复制 config/recorder_cleanup.json.example 并改 root"),
        ("config.livedeck", "config/livedeck.env", "直播台登录态（可选）",
         "不用多屏直播台可以不管；要用就跑 python run_livedeck.py 然后扫码"),
    ):
        p = root / rel
        if not p.is_file():
            report.add(key, WARN, title, "未配置", hint)
        else:
            report.add(key, OK, title, f"{p.stat().st_size} 字节")

    # --- 网络代理（可选）：config/proxy.json 由页面「设置 → 网络代理」生成，这里只校验内容
    p = root / "config" / "proxy.json"
    if not p.is_file():
        report.add("config.proxy", WARN, "网络代理配置（可选）", "未配置（使用系统代理）",
                   "需要代理时在监控台 / 监控室右上角「设置 → 网络代理」里填，保存后自动生成")
    else:
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            from shared import proxy as proxy_cfg
            if not isinstance(raw, dict):
                raise ValueError("顶层必须是对象")
            cfg = proxy_cfg.validate(raw)
        except (OSError, ValueError, TypeError) as exc:
            report.add("config.proxy", FAIL, "网络代理配置", f"内容不合法：{exc}",
                       "回到「设置 → 网络代理」重填地址并保存，或删掉 config/proxy.json（等于不走代理）")
        else:
            detail = {
                "custom": f"自定义 {cfg['url']}（no_proxy {cfg['no_proxy']}）",
                "none": "不使用代理（强制直连）",
                "system": "使用系统代理",
            }.get(cfg["mode"], cfg["mode"])
            report.add("config.proxy", OK, "网络代理配置", detail)


def check_dirs(report: Report, root: Path) -> None:
    for rel in ("data", "logs", "out", "config"):
        ok, detail = _can_write(root / rel)
        report.add(f"dirs.{rel}", OK if ok else FAIL, f"目录可写 {rel}/",
                   "" if ok else detail,
                   "" if ok else "检查目录权限，或删掉同名文件（它可能是个文件而不是目录）")


def check_database(report: Report, root: Path) -> None:
    db = root / "data" / "bot.db"
    code_ver = _code_schema_version()
    if not db.is_file():
        report.add("db.schema", OK, "数据库 data/bot.db", "还没有库，首次启动会自动创建",
                   f"代码支持 schema v{code_ver}" if code_ver else "")
        return
    current, err = _db_schema(db)
    if current is None:
        report.add("db.schema", FAIL, "数据库 data/bot.db", f"{db.stat().st_size} 字节但打不开：{err}",
                   "确认文件没被别的程序独占；损坏时用备份恢复")
        return
    if code_ver and current > code_ver:
        report.add("db.schema", FAIL, "数据库 schema 版本", f"库里是 v{current}，代码只支持到 v{code_ver}",
                   "换用更新版本的程序，或恢复旧库备份（旧代码不会降级迁移）")
    else:
        report.add("db.schema", OK, "数据库 schema 版本", f"v{current}（代码支持 v{code_ver or '?'}）",
                   "" if current == code_ver else "首次启动会把库迁移到最新版本")


def check_ports(report: Report, port: int) -> None:
    ok, detail = _port_free("127.0.0.1", port)
    report.add("port.web", OK if ok else WARN, f"管理台端口 {port}", detail,
               "" if ok else "如果机器人已经在跑，这是正常的；否则先停掉占用端口的进程")


def check_files(report: Report, root: Path) -> None:
    for rel in ("VERSION", "CHANGELOG.md", "README.md", "requirements.txt"):
        p = root / rel
        report.add(f"files.{rel}", OK if p.is_file() else FAIL, f"文件 {rel}",
                   "存在" if p.is_file() else "缺失（发布包不完整？）")


# ---------------------------------------------------------------- 入口

def run(root: Path | None = None, port: int = 8765) -> Report:
    root = Path(root or ROOT)
    report = Report(root=str(root))
    try:
        from shared.version import read_version
        report.version = read_version(root / "VERSION")
    except Exception:
        report.version = "unknown"
    check_python(report, root)
    check_dependencies(report)
    check_files(report, root)
    check_configs(report, root)
    check_dirs(report, root)
    check_database(report, root)
    check_ports(report, port)
    return report


def render(report: Report) -> str:
    lines = [f"环境自检 v{report.version}  项目根目录 {report.root}", ""]
    for c in report.checks:
        head = f"{_MARK.get(c.level, '[????]')} {c.title}"
        if c.detail:
            head += f"：{c.detail}"
        lines.append(head)
        if c.hint and c.level != OK:
            lines.append(f"          → {c.hint}")
    ok = sum(c.level == OK for c in report.checks)
    lines.append("")
    lines.append(f"通过 {ok} / 警告 {len(report.warnings)} / 阻塞 {len(report.failures)}")
    if report.failures:
        lines.append("有阻塞项：按上面的 → 提示处理完再启动。")
    else:
        pending = [c for c in report.warnings if c.key in {"config.bbot", "config.p0ids", "config.subjects"}]
        if pending:
            lines.append("依赖就绪，还差账号接入：打开监控台，按右上角「接入指引」走一遍"
                         "（命令行等价：python scripts/setup_wizard.py）")
        else:
            lines.append("没有阻塞项，可以启动：python scripts/run_console.py --open"
                         "（采集 / 推送进程由它按绑定状态自动托管）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="环境自检：只读检查依赖、配置、数据库、端口")
    ap.add_argument("--json", action="store_true", help="输出 JSON（给安装脚本/CI 用）")
    ap.add_argument("--strict", action="store_true", help="警告也当失败")
    ap.add_argument("--port", type=int, default=8765, help="管理台端口（默认 8765）")
    ap.add_argument("--root", default=str(ROOT), help="项目根目录（默认按脚本位置推断）")
    args = ap.parse_args(argv)

    try:
        report = run(Path(args.root), args.port)
    except Exception as exc:  # 自检自己崩了也要给出可读结果
        print(f"[FAIL] 自检过程异常：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(render(report))

    if report.failures:
        return 1
    if args.strict and report.warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
