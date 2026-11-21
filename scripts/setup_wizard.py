"""接入向导：把自己的 QQ 机器人、B 站账号接进这个项目。

它只做你本来要手动做的事（写 BBOT.env / 跑扫码登录 / 抓 openid / 自检），
不会替你去申请机器人，也不会上传任何东西。

用法：
  python scripts/setup_wizard.py             # 交互向导（默认跑全部步骤）
  python scripts/setup_wizard.py --qq        # 只配 QQ 机器人凭据
  python scripts/setup_wizard.py --bili      # 只做 B 站扫码登录
  python scripts/setup_wizard.py --ids       # 只抓推送目标 openid
  python scripts/setup_wizard.py --check     # 只跑环境自检
  python scripts/setup_wizard.py --list      # 只打印当前配置状态

每一步都可以跳过；已经配好的会先显示现状，问你要不要覆盖。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BBOT = ROOT / "BBOT.env"
BILI_ENV = ROOT / "config" / "bili.env"
IDS = ROOT / ".p0_ids.json"
SUBJECTS = ROOT / "config" / "subjects.json"

BBOT_HEADER = """# QQ 官方机器人凭据（本文件不要提交、不要发给别人）
# 在 https://q.qq.com/ 的机器人后台「开发设置」里拿 AppID / AppSecret
"""


# ---------------------------------------------------------------- 小工具

def say(msg: str = "") -> None:
    print(msg)


def head(title: str) -> None:
    print()
    print("=" * 62)
    print(f" {title}")
    print("=" * 62)


def ask(prompt: str, default: str = "") -> str:
    suffix = f"（默认 {default}）" if default else ""
    try:
        raw = input(f"   {prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return raw or default


def ask_secret(prompt: str) -> str:
    import getpass
    try:
        return getpass.getpass(f"   {prompt}（输入时不回显）: ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        raw = input(f"   {prompt} {hint} ").strip().lower()
    except EOFError:
        return default
    if not raw:
        return default
    return raw in ("y", "yes", "是", "好", "1")


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def write_env(path: Path, updates: dict[str, str], header: str = "") -> None:
    """更新 env 文件里的键，保留原有注释与其它行。"""
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    elif header:
        lines = header.rstrip("\n").splitlines()

    remaining = dict(updates)
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key = s.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    for key, value in remaining.items():
        lines.append(f"{key}={value}")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    try:  # 尽量收紧权限；失败不影响流程
        os.chmod(path, 0o600)
    except OSError:
        pass


def mask(value: str, keep: int = 4) -> str:
    if not value:
        return "（空）"
    if len(value) <= keep * 2:
        return value[0] + "*" * max(len(value) - 1, 0)
    return value[:keep] + "*" * 6 + value[-3:]


def run_step(cmd: list[str], timeout: int | None = None) -> int:
    """用当前解释器跑一个项目脚本，返回退出码。"""
    say(f"   → {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), timeout=timeout)
        return proc.returncode
    except subprocess.TimeoutExpired:
        return 124
    except OSError as exc:
        say(f"   [失败] 启动失败：{exc}")
        return 1


# ---------------------------------------------------------------- 状态总览

def status() -> dict[str, object]:
    bbot = load_env(BBOT)
    bili = load_env(BILI_ENV)
    openid = ""
    if IDS.is_file():
        try:
            openid = str(json.loads(IDS.read_text(encoding="utf-8")).get("user_openid") or "")
        except (OSError, ValueError):
            openid = ""
    n_subjects = 0
    if SUBJECTS.is_file():
        try:
            n_subjects = len(json.loads(SUBJECTS.read_text(encoding="utf-8")).get("subjects") or [])
        except (OSError, ValueError):
            n_subjects = -1
    return {
        "app_id": bbot.get("QQBOT_APP_ID", ""),
        "app_secret": bbot.get("QQBOT_APP_SECRET", ""),
        "sessdata": bili.get("SESSDATA", ""),
        "openid": openid,
        "subjects": n_subjects,
    }


def print_status() -> None:
    st = status()
    say("")
    say("当前状态：")
    say(f"   QQ 机器人 AppID   : {st['app_id'] or '未配置'}")
    say(f"   QQ 机器人 Secret  : {mask(str(st['app_secret']))}")
    say(f"   B 站 SESSDATA     : {mask(str(st['sessdata']))}")
    say(f"   推送目标 openid   : {str(st['openid'])[:6] + '****' if st['openid'] else '未配置'}")
    n = int(st["subjects"])
    say(f"   主播名单          : {'JSON 解析失败' if n < 0 else (str(n) + ' 个' if n else '未配置')}")


# ---------------------------------------------------------------- 步骤 1：QQ

def step_qq(force: bool = False) -> bool:
    head("第 1 步 / 共 4 步：接入你自己的 QQ 官方机器人")
    env = load_env(BBOT)
    has_id = bool(env.get("QQBOT_APP_ID"))
    has_secret = bool(env.get("QQBOT_APP_SECRET"))

    if has_id and has_secret and not force:
        say(f"   已配置：AppID {env.get('QQBOT_APP_ID')}，Secret {mask(env.get('QQBOT_APP_SECRET'))}")
        if not ask_yes_no("要换成别的机器人吗？", default=False):
            return True

    say("")
    say("   还没有机器人？按这个顺序做（约 5 分钟）：")
    say("     1. https://q.qq.com/ 用你的 QQ 登录「QQ 开放平台」")
    say("     2. 创建机器人应用 → 开发设置 → 复制 AppID / AppSecret")
    say("     3. 沙箱配置里把你自己加进去，并开启「私聊消息」权限")
    say("     4. 事件订阅选 WebSocket，勾选 C2C 消息事件")
    say("")

    app_id = ask("AppID（纯数字）", str(env.get("QQBOT_APP_ID", "")))
    if not app_id:
        say("   [跳过] 没填 AppID，这一步先不做了")
        return False
    if not app_id.isdigit():
        say("   [警告] AppID 一般是纯数字，你填的看起来不太对；先照原样保存")

    secret = ask_secret("AppSecret")
    if not secret:
        secret = str(env.get("QQBOT_APP_SECRET", ""))
        if not secret:
            say("   [跳过] 没填 AppSecret，这一步先不做了")
            return False
        say("   [提示] 沿用文件里已有的 AppSecret")

    write_env(BBOT, {"QQBOT_APP_ID": app_id, "QQBOT_APP_SECRET": secret}, header=BBOT_HEADER)
    say(f"   已写入 {BBOT.relative_to(ROOT)}（权限 600）")

    # 顺手验一次鉴权，失败只提示不影响保存
    if ask_yes_no("现在联网验证一次凭据？", default=True):
        code = run_step([sys.executable, str(ROOT / "scripts" / "p0_check.py"), "token"])
        if code == 0:
            say("   [OK] 凭据有效，能拿到 access_token")
        else:
            say("   [提示] 验证没通过。常见原因：Secret 复制错、机器人未发布到沙箱、网络需要代理")
    return True


# ---------------------------------------------------------------- 步骤 2：B 站

def step_bili() -> bool:
    head("第 2 步 / 共 4 步：接入你自己的 B 站账号（可选但推荐）")
    env = load_env(BILI_ENV)
    if env.get("SESSDATA"):
        say(f"   已配置：SESSDATA {mask(env.get('SESSDATA'))}")
        if not ask_yes_no("要重新扫码登录吗？", default=False):
            return True

    say("")
    say("   为什么要配：登录态下动态接口很少撞 -352，图文正文更完整。")
    say("   建议用只读小号；不配也能跑（匿名模式），只是偶尔取不到数据。")
    say("")
    if not ask_yes_no("现在开始扫码登录（终端里出二维码，用 B 站 App 扫）？", default=True):
        say("   [跳过] 以后想配再跑：python scripts/bili_qr_login.py")
        return False

    code = run_step([sys.executable, str(ROOT / "scripts" / "bili_qr_login.py")])
    if code != 0:
        say("   [提示] 扫码登录没完成（超时/取消）。可以重跑，或在管理台「B 站登录态」页扫码")
        return False
    say("   [OK] 登录态已写入 config/bili.env")
    return True


# ---------------------------------------------------------------- 步骤 3：openid

def step_ids(wait_seconds: int = 120) -> bool:
    head("第 3 步 / 共 4 步：抓取推送目标（你自己的 QQ openid）")
    st = status()
    if st["openid"]:
        say(f"   已配置：openid {str(st['openid'])[:6]}****{str(st['openid'])[-4:]}")
        if not ask_yes_no("要重新抓一次吗？", default=False):
            return True

    say("")
    say("   机器人只能给「跟它说过话的人」主动发消息，所以要先把你的 openid 记下来。")
    say(f"   流程：下面会在后台监听 {wait_seconds} 秒 → 你打开 QQ 私聊机器人随便发一句话 →")
    say("         收到后自动关掉并把 openid 写进 .p0_ids.json")
    say("")
    if not ask_yes_no("准备好了？", default=True):
        say("   [跳过] 以后想抓再跑：python scripts/p0_check.py listen")
        return False

    say("")
    say("   >>> 现在去 QQ 里私聊你的机器人，发一句「你好」 <<<")
    say("")
    code = run_step([sys.executable, str(ROOT / "scripts" / "p0_check.py"), "listen"], timeout=wait_seconds)
    if code == 124:
        say(f"   [超时] {wait_seconds} 秒内没收到消息。确认：机器人已发布到沙箱、你私聊的是它、事件订阅是 WebSocket")
        return False
    st = status()
    if st["openid"]:
        say(f"   [OK] 已抓到 openid：{str(st['openid'])[:6]}****{str(st['openid'])[-4:]}")
        return True
    say("   [失败] 没读到 openid，检查上面的输出")
    return False


# ---------------------------------------------------------------- 步骤 4：名单

def step_subjects() -> bool:
    head("第 4 步 / 共 4 步：要监控谁（主播名单）")
    st = status()
    n = int(st["subjects"])
    if n < 0:
        say("   [问题] config/subjects.json 不是合法 JSON，先修好它")
        return False
    if n > 0:
        say(f"   已有 {n} 个主播")
        if not ask_yes_no("要现在打开文件手动改吗？", default=False):
            return True

    say("")
    say("   名单文件：config/subjects.json（格式见 config/subjects.json.example）")
    say("   一个主播最少要有 uid；room_id 不确定就写 0。")
    say("   想一次导入你 B 站「关注列表」里的全部人（需要上面第 2 步的登录态）：")
    say("     python scripts/import_follows.py --help")
    say("")
    if ask_yes_no("用记事本打开名单文件编辑？", default=False):
        target = SUBJECTS if SUBJECTS.is_file() else ROOT / "config" / "subjects.json.example"
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
        except Exception as exc:
            say(f"   [提示] 打不开：{exc}；自己用编辑器打开 {target}")
    return True


# ---------------------------------------------------------------- 收尾自检

def final_check() -> None:
    head("收尾：环境自检")
    run_step([sys.executable, str(ROOT / "scripts" / "preflight.py")])
    say("")
    say("   下一步：")
    say(r"     runtime\python.exe scripts\run_bot.py --channel qq --web")
    say("   然后浏览器打开 http://127.0.0.1:8765 看管理台。")
    say("   开机自启、升级、备份见 docs/31-发布与交接手册.md")


# ---------------------------------------------------------------- 入口

def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="把 QQ 机器人和 B 站账号接进本项目")
    ap.add_argument("--qq", action="store_true", help="只跑 QQ 凭据")
    ap.add_argument("--bili", action="store_true", help="只跑 B 站扫码登录")
    ap.add_argument("--ids", action="store_true", help="只抓推送目标 openid")
    ap.add_argument("--check", action="store_true", help="只跑环境自检")
    ap.add_argument("--list", action="store_true", help="只打印当前配置状态")
    ap.add_argument("--force", action="store_true", help="已配置的步骤也重新问一遍")
    ap.add_argument("--wait", type=int, default=120, help="抓 openid 的监听秒数（默认 120）")
    args = ap.parse_args(argv)

    if args.list:
        print_status()
        return 0
    if args.check:
        final_check()
        return 0

    head(f"接入向导（项目 v{(ROOT / 'VERSION').read_text(encoding='utf-8').strip() if (ROOT / 'VERSION').is_file() else '?'}）")
    say("   这个向导只写本机文件，不改别人的东西，也不上传数据。")
    say("   每一步都能跳过；中途 Ctrl+C 不会留下半截配置（写入是原子的）。")
    print_status()

    only = args.qq or args.bili or args.ids
    try:
        if args.qq or not only:
            step_qq(force=args.force)
        if args.bili or not only:
            step_bili()
        if args.ids or not only:
            step_ids(wait_seconds=max(15, args.wait))
        if not only:
            step_subjects()
    except KeyboardInterrupt:
        say("")
        say("   已中断（已写入的配置都完整，不会半截）。")
        return 130

    final_check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
