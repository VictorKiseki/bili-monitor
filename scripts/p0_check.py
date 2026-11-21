"""QQ 官方机器人接入验证（V1 鉴权 / V2 收事件拿 openid / V3 主动发送）。

真正的实现都在 channels/qq_setup.py —— 管理台「机器人」页用的是同一份逻辑，
这里只是把结果打印成人能看的命令行版本。

凭据来源（按优先级）：进程环境变量 → BBOT.env → .env → bbot.env → config/.env

用法：
  python scripts/p0_check.py token                  # V1 只验鉴权
  python scripts/p0_check.py listen                 # V2 连 WS，等你在私聊 / 群里@它，打印 openid
  python scripts/p0_check.py send-group "你好"       # V3 向群主动发一条，打印接口耗时
  python scripts/p0_check.py send-user  "你好"       # V3 向你私聊主动发一条

V2 拿到的 openid 会写到 .p0_ids.json，V3 直接读它。
管理台也能做同样的事：127.0.0.1:8765 →「机器人」页。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from channels import qq_setup as Q  # noqa: E402


def die(msg: str, hint: str = "") -> "NoReturn":  # type: ignore[name-defined]
    print("[FAIL] " + msg)
    if hint:
        for line in str(hint).splitlines():
            print("       " + line)
    raise SystemExit(2)


def load_env() -> tuple[str, str]:
    """兼容旧调用方：返回 (app_id, secret)，并打印用的是哪个文件。"""
    c = Q.credentials(ROOT)
    if c["source"]:
        print("[凭据文件] %s" % Path(c["source"]).name)
    return c["app_id"], c["secret"]


def cmd_token(app_id: str, secret: str) -> int:
    try:
        r = Q.verify(app_id, secret)
    except Q.QQError as e:
        die(e.message, e.hint)
    bot = r.get("bot") or {}
    print("[ok] 鉴权通过  expires_in=%ss  耗时=%sms" % (r.get("expires_in"), r.get("ms")))
    if bot.get("username"):
        print("[ok] 机器人：%s（id=%s）" % (bot["username"], bot.get("id") or "-"))
    print("\nV1 通过。可以继续 listen。")
    return 0


def cmd_listen(app_id: str, secret: str, timeout: float) -> int:
    listener = Q.OpenidListener(app_id, secret, Q.ids_path(ROOT), timeout=timeout)
    try:
        listener.start()
    except Q.QQError as e:
        die(e.message, e.hint)
    print("\n>>> 现在去私聊机器人发一句话（群里 @ 它也可以）。Ctrl+C 结束。\n")
    seen = 0
    last_stage = ""
    try:
        while True:
            st = listener.status()
            if st["stage"] != last_stage and st["stage"] in ("connecting", "waiting", "done",
                                                            "error", "stopped"):
                last_stage = st["stage"]
                if st["stage"] == "waiting":
                    print("[ok] 已连上网关，等待消息…")
                    if st.get("bot"):
                        print("[ok] 机器人：%s" % st["bot"])
            evs = listener.status()
            if evs.get("last_event") and seen < (evs.get("events") or 0):
                seen = evs.get("events") or 0
                e = evs["last_event"]
                print("[%s] openid=%s  内容=%s" % (e.get("where") or e.get("kind"),
                                                   e.get("openid"), e.get("content") or ""))
                print("      已写入 %s（user_openid=%s group_openid=%s）"
                      % (Q.IDS_FILE_NAME, evs.get("user_openid") or "-",
                         evs.get("group_openid") or "-"))
            if st["stage"] in ("done", "error", "stopped"):
                if st["stage"] == "error":
                    print("[FAIL] " + st["error"])
                    if st.get("hint"):
                        print("       " + st["hint"])
                    return 2
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[停止] 已拿到的 openid 在 %s，没拿到就再跑一次" % Q.IDS_FILE_NAME)
    finally:
        listener.stop()
    st = listener.status()
    ids_txt = ", ".join(
        "%s=%s" % (k, v) for k, v in (("user_openid", st.get("user_openid")),
                                      ("group_openid", st.get("group_openid"))) if v)
    if st.get("events"):
        print("[ok] 本次收到 %d 条消息，已保存：%s" % (st["events"], ids_txt))
        print("V2 通过。接着跑 send-user / send-group。")
        return 0
    if ids_txt:
        print("[ok] 已有绑定：%s" % ids_txt)
        print("     这次没收到新消息（沿用已保存的值）；要换号就再跑一次，并用新号发一句话。")
        print("V2 可用。接着跑 send-user / send-group。")
        return 0
    if st.get("hint"):
        print("[提示] " + st["hint"])
    print("[未完成] 没有收到任何消息；确认已私聊机器人（不是群里发普通消息）")
    return 1


def cmd_send(kind: str, content: str, app_id: str, secret: str) -> int:
    ids = Q.read_ids(Q.ids_path(ROOT))
    key = "group_openid" if kind == "group" else "user_openid"
    if not ids.get(key):
        die("缺少 %s" % key, "先跑：python scripts/p0_check.py listen（或在管理台「机器人」页等一条消息）")
    try:
        r = Q.send_text(app_id, secret, str(ids[key]), kind=kind, content=content)
    except Q.QQError as e:
        die(e.message, e.hint)
    print("[ok] 发送成功 status=%s 接口耗时=%sms" % (r["status"], r["ms"]))
    if r.get("id"):
        print("     返回 id=%s" % r["id"])
    print("     >>> 现在看手机：记下「发出 → 手机响」的秒数。")
    print("\nV3 通过。")
    return 0


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["token", "listen", "send-group", "send-user"])
    ap.add_argument("text", nargs="?", default="B站监控测试消息")
    ap.add_argument("--timeout", type=float, default=0,
                    help="listen 等多少秒后退出；0 = 一直等（Ctrl+C 停）")
    args = ap.parse_args()

    app_id, secret = load_env()
    if not app_id or not secret:
        die("没有找到凭据",
            "在项目根目录 BBOT.env 写两行（或跑 python scripts/setup_wizard.py --qq）：\n"
            "QQBOT_APP_ID=你的AppID\n"
            "QQBOT_APP_SECRET=你的AppSecret\n"
            "也可以在管理台「机器人」页填。")
    print("[凭据] AppID=%s  Secret=%s" % (Q.mask(app_id), Q.mask(secret)))

    if args.action == "token":
        return cmd_token(app_id, secret)
    if args.action == "listen":
        return cmd_listen(app_id, secret, args.timeout)
    return cmd_send("group" if args.action == "send-group" else "user",
                    args.text, app_id, secret)


if __name__ == "__main__":
    raise SystemExit(main())
