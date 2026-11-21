"""关注流测试路径的登录：拿到可写（关注/取关）的独立凭据。

和 scripts/bili_qr_login.py 的区别
---------------------------------
- 它给采集进程写 config/bili.env，只需要 SESSDATA（只读动态）；
- 本脚本给「关注流测试路径」写 config/bili_follow.env，需要 SESSDATA + bili_jct
  （bili_jct 是 /x/relation/modify 的 CSRF token，SESSDATA 推不出来，必须登录拿）；
- 两个文件互不影响：现有监控进程继续读 config/bili.env，测试路径只读
  config/bili_follow.env，随时可以删掉后者回滚。

用法：
    python scripts/follow_feed_login.py                 # 终端扫码 + 存 SVG，写 config/bili_follow.env
    python scripts/follow_feed_login.py --cookie "SESSDATA=...; bili_jct=...; DedeUserID=..."
    python scripts/follow_feed_login.py --no-save       # 只登录验证，不落盘

扫码成功后会做三件事：nav 确认账号、用「关注自己」验证 bili_jct（预期 22003，
B 站不允许关注自己；若 -111 说明 token 无效）、读一次关注总数。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector import bili_login  # noqa: E402
from collector.bili_login import (  # noqa: E402
    QR_POLL, QR_STATE, _client, _cookies_from_crossdomain,
    _cookies_from_set_cookie, _verify, qr_terminal, start_login,
)
from collector.follow_feed import (  # noqa: E402
    DEFAULT_ENV, FollowCookies, FollowFeedClient, load_follow_cookies,
    parse_cookie_string, save_follow_cookies,
)


def qr_png(text: str, path: str, border: int = 4, scale: int = 8) -> Path:
    """把二维码渲染成 PNG，方便直接贴到聊天窗口/图片查看器里扫。"""
    from PIL import Image, ImageDraw

    from collector.qrcodegen import QrCode

    qr = QrCode.encode_text(text, QrCode.Ecc.MEDIUM)
    size = qr.get_size()
    dim = (size + border * 2) * scale
    img = Image.new("RGB", (dim, dim), "white")
    draw = ImageDraw.Draw(img)
    for y in range(size):
        for x in range(size):
            if qr.get_module(x, y):
                x0, y0 = (x + border) * scale, (y + border) * scale
                draw.rectangle([x0, y0, x0 + scale - 1, y0 + scale - 1], fill="black")
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    return out


def poll_login_full(qrcode_key: str) -> dict:
    """和 bili_login.poll_login 同流程，但把整组 cookie（含 bili_jct）带回来。"""
    key = (qrcode_key or "").strip()
    if not key:
        return {"status": "error", "message": "qrcode_key 为空"}
    try:
        with _client() as c:
            r = c.get(QR_POLL, params={"qrcode_key": key})
            body = r.json()
            cookies = _cookies_from_set_cookie(r)
    except Exception as e:                       # noqa: BLE001
        return {"status": "error", "message": "查询扫码状态失败：%s %s"
                % (e.__class__.__name__, e)}

    if body.get("code") != 0:
        return {"status": "error", "message": "B 站返回 code=%s %s"
                % (body.get("code"), body.get("message"))}
    data = body.get("data") or {}
    code = int(data.get("code") or 0)
    if code != 0:
        state, msg = QR_STATE.get(code, ("error", "未知扫码状态 code=%s" % code))
        return {"status": state, "message": data.get("message") or msg, "code": code}

    if not cookies.get("SESSDATA"):
        cookies = {**_cookies_from_crossdomain(data.get("url") or ""), **cookies}
    sess = cookies.get("SESSDATA", "")
    if not sess:
        return {"status": "error", "message": "扫码成功但没拿到 SESSDATA"}

    ok, uname, mid, msg = _verify(sess)
    if not ok:
        return {"status": "error", "message": "登录成功但校验不通过：%s" % msg}
    from collector.bili import mask_sessdata
    return {
        "status": "success",
        "message": "扫码登录成功，账号 %s（mid %s）" % (uname or "-", mid),
        "cookies": cookies,
        "sessdata": sess,
        "bili_jct": cookies.get("bili_jct", ""),
        "dede_user_id": cookies.get("DedeUserID", ""),
        "uname": uname,
        "mid": mid,
        "masked": mask_sessdata(sess),
    }


def report_and_save(cookies: FollowCookies, env: str, no_save: bool,
                    qr_svg: str = "") -> int:
    if not cookies.sessdata:
        print("[失败] 没有 SESSDATA，无法使用")
        return 1
    if not cookies.bili_jct:
        print("[警告] 这次登录没拿到 bili_jct：可以读关注流，但不能关注/取关。")
        print("       若终端二维码之外还想补，可用 --cookie 粘贴整串 cookie。")

    client = FollowFeedClient(cookies, min_interval=1.5)
    info = client.login_info()
    print("      nav：", info.get("uname"), "mid", info.get("mid"),
          "" if info.get("ok") else "（未登录！）")
    if not info.get("ok"):
        return 1

    if info.get("can_follow"):
        csrf = client.check_csrf()
        ok = bool(csrf.get("ok"))
        print("      bili_jct 校验：", "通过（%s %s）" % (csrf.get("code"), csrf.get("message"))
              if ok else "失败（%s %s）" % (csrf.get("code"), csrf.get("message")))
        if not ok:
            print("      提示：token 可能过期，重新扫码；或检查系统时间")
            return 1
    else:
        print("      bili_jct：缺失，关注/取关不可用")

    try:
        follows, total = client.followings()
        print("      当前关注：%d 人（列表返回 %d）" % (total, len(follows)))
    except Exception as e:                       # noqa: BLE001
        print("      关注列表读取失败：%s" % e)

    if no_save:
        print("      --no-save：没有写任何文件")
        return 0
    p = save_follow_cookies(env, cookies, uname=info.get("uname") or "",
                            mid=int(info.get("mid") or 0))
    print("      已写入", p, "（权限 600，含 bili_jct，勿外发）")
    if qr_svg:
        print("      二维码留档：", qr_svg)
    print("      下一步：python scripts/probe_follow_feed.py status")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="关注流测试路径登录（SESSDATA + bili_jct）")
    ap.add_argument("--env", default=str(ROOT / DEFAULT_ENV), help="凭据文件路径")
    ap.add_argument("--cookie", default="", help="直接粘贴整串 cookie（跳过扫码）")
    ap.add_argument("--svg", default=str(ROOT / "logs" / "follow_login_qr.svg"),
                    help="二维码 SVG 保存路径（默认 logs/follow_login_qr.svg）")
    ap.add_argument("--png", default=str(ROOT / "logs" / "follow_login_qr.png"),
                    help="二维码 PNG 保存路径（默认 logs/follow_login_qr.png）")
    ap.add_argument("--no-save", action="store_true", help="只验证，不写配置")
    ap.add_argument("--interval", type=float, default=2.0, help="轮询间隔（秒）")
    args = ap.parse_args()

    if args.cookie.strip():
        raw = parse_cookie_string(args.cookie)
        cookies = FollowCookies(
            sessdata=raw.get("SESSDATA", ""),
            bili_jct=raw.get("bili_jct", ""),
            dede_user_id=raw.get("DedeUserID", ""),
            source="cli:--cookie",
        )
        print("[1/1] 使用粘贴的 cookie，SESSDATA", cookies.masked or "(空)")
        return report_and_save(cookies, args.env, args.no_save)

    print("[1/3] 申请二维码…")
    s = start_login()
    print(qr_terminal(s["url"]))
    print("      内容：", s["url"])
    if args.svg:
        p = Path(args.svg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(s["svg"], "utf-8")
        print("      二维码 SVG：", p)
    if args.png:
        try:
            print("      二维码 PNG：", qr_png(s["url"], args.png), "（可直接在聊天里扫）")
        except Exception as e:                   # noqa: BLE001
            print("      PNG 生成失败（不影响扫码）：", e)
    print("[2/3] 用手机 B 站 App 扫码并确认（%d 秒内有效）" % s["expires_in"])

    last = None
    deadline = time.time() + s["expires_in"] + 10
    while time.time() < deadline:
        try:
            r = poll_login_full(s["qrcode_key"])
        except KeyboardInterrupt:
            print("\n[取消] 已中断，没有写入任何配置")
            return 130
        st = r.get("status")
        if st != last:
            print("      →", r.get("message") or st)
            last = st
        if st == "success":
            print("[3/3]", r["message"])
            cookies = FollowCookies(
                sessdata=r["sessdata"],
                bili_jct=r.get("bili_jct", ""),
                dede_user_id=r.get("dede_user_id", ""),
                source="qr:follow_feed",
            )
            return report_and_save(cookies, args.env, args.no_save, qr_svg=args.svg)
        if st in ("expired", "error"):
            print("[失败]", r.get("message"))
            return 1
        time.sleep(max(0.5, args.interval))
    print("[超时] 二维码已过期，重新运行本脚本")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
