"""B 站扫码登录：让采集进程自己持有一份登录态（不依赖任何浏览器）。

为什么需要这个模块
------------------
浏览器里拿到的 SESSDATA 是“那台浏览器里那次登录”的凭据：在浏览器点退出登录、
换小号/换大号、清理站点数据，服务端都会把那次登录的 SESSDATA 吊销。于是只要
你用同一个浏览器切账号，监控这边的登录态就跟着失效。

扫码登录走的是 passport 的独立设备会话：B 站把一份新的 SESSDATA 发给本进程，
它与浏览器里的登录互不相干。之后无论在浏览器里退出还是换号，这份 SESSDATA 都
不受影响（只有它自己的长期过期，一般数月）。

对外接口
--------
    start_login()  -> {"url", "qrcode_key", "expires_in", "svg"}
    poll_login(k)  -> {"status": waiting|scanned|expired|success|error, "message", ...}
    write_env_sessdata(path, sessdata, uname, mid, source)  -> Path

只做登录与本地配置读写，不点赞、不评论、不碰任何写接口。
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

from collector.qrcodegen import QrCode

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
PASSPORT = "https://passport.bilibili.com"
API = "https://api.bilibili.com"
QR_GENERATE = PASSPORT + "/x/passport-login/web/qrcode/generate"
QR_POLL = PASSPORT + "/x/passport-login/web/qrcode/poll"
NAV = API + "/x/web-interface/nav"

# poll 接口 data.code 的取值（顶层 code 才是接口状态）
QR_STATE = {
    86101: ("waiting", "等待扫码"),
    86090: ("scanned", "已扫码，请在手机上确认"),
    86038: ("expired", "二维码已失效"),
}

COOKIE_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5")


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=12.0,
        headers={
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        follow_redirects=False,
    )


# ---------- 二维码渲染 ----------
def qr_svg(text: str, border: int = 4, scale: int = 6) -> str:
    """把文本编码成 SVG 二维码（白底黑块，浏览器里直接 innerHTML 就能显示）。"""
    qr = QrCode.encode_text(text, QrCode.Ecc.LOW)
    size = qr.get_size()
    dim = (size + border * 2) * scale
    path = []
    for y in range(size):
        for x in range(size):
            if qr.get_module(x, y):
                path.append(f"M{(x + border) * scale} {(y + border) * scale}h{scale}v{scale}h-{scale}z")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{dim}" height="{dim}" '
        f'viewBox="0 0 {dim} {dim}" shape-rendering="crispEdges">'
        f'<rect width="{dim}" height="{dim}" fill="#ffffff"/>'
        f'<path d="{"".join(path)}" fill="#000000"/></svg>'
    )


def qr_terminal(text: str, border: int = 2) -> str:
    """终端里打印二维码：每格两个空格 + ANSI 背景色（白底黑块，扫码器能识别的正色）。"""
    qr = QrCode.encode_text(text, QrCode.Ecc.LOW)
    size = qr.get_size()
    light, dark = "\x1b[48;5;231m  ", "\x1b[48;5;16m  "
    lines = []
    for y in range(-border, size + border):
        row = []
        for x in range(-border, size + border):
            row.append(dark if (0 <= x < size and 0 <= y < size and qr.get_module(x, y)) else light)
        lines.append("".join(row) + "\x1b[0m")
    return "\n".join(lines)


# ---------- 登录流程 ----------
def start_login() -> dict[str, Any]:
    """申请一个扫码会话。返回二维码内容、轮询密钥与 SVG。"""
    try:
        with _client() as c:
            body = c.get(QR_GENERATE).json()
    except httpx.HTTPError as e:
        raise RuntimeError("申请二维码失败：网络错误 %s" % e.__class__.__name__) from e
    if body.get("code") != 0:
        raise RuntimeError("申请二维码失败：code=%s %s" % (body.get("code"), body.get("message")))
    data = body.get("data") or {}
    url, key = data.get("url") or "", data.get("qrcode_key") or ""
    if not (url and key):
        raise RuntimeError("申请二维码失败：返回里没有 url/qrcode_key")
    return {"url": url, "qrcode_key": key, "expires_in": 180, "svg": qr_svg(url)}


def _cookies_from_set_cookie(r: httpx.Response) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in r.headers.get_list("set-cookie"):
        name, _, rest = raw.partition("=")
        val = rest.split(";", 1)[0].strip()
        name = name.strip()
        if name in COOKIE_KEYS and val:
            out[name] = val
    return out


def _cookies_from_crossdomain(url: str) -> dict[str, str]:
    """兜底：登录成功时 data.url 是跨域登录链接，参数里带着同一批 cookie。"""
    out: dict[str, str] = {}
    if not url:
        return out
    from urllib.parse import parse_qs, urlparse
    for k, vs in parse_qs(urlparse(url).query).items():
        if k in COOKIE_KEYS and vs and vs[0]:
            out[k] = vs[0]
    return out


def _verify(sessdata: str) -> tuple[bool, str, int, str]:
    """用 nav 接口确认这份 SESSDATA 真的能登录，返回 (ok, uname, mid, message)。"""
    try:
        with _client() as c:
            body = c.get(NAV, headers={"Cookie": "SESSDATA=" + sessdata}).json()
    except httpx.HTTPError as e:
        return False, "", 0, "校验失败：网络错误 %s" % e.__class__.__name__
    data = body.get("data") or {}
    if body.get("code") == 0 and data.get("isLogin"):
        return True, data.get("uname") or "", int(data.get("mid") or 0), "有效"
    if body.get("code") == 0:
        return False, "", 0, "nav 通了但 isLogin=false（SESSDATA 没生效）"
    return False, "", 0, "nav code=%s %s" % (body.get("code"), body.get("message"))


def poll_login(qrcode_key: str) -> dict[str, Any]:
    """轮询扫码状态。成功后不落盘，由调用方决定写哪里。"""
    key = (qrcode_key or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{16,64}", key):
        return {"status": "error", "message": "qrcode_key 不合法"}
    try:
        with _client() as c:
            r = c.get(QR_POLL, params={"qrcode_key": key})
            body = r.json()
            cookies = _cookies_from_set_cookie(r)
    except httpx.HTTPError as e:
        return {"status": "error", "message": "查询扫码状态失败：网络错误 %s" % e.__class__.__name__}
    except ValueError:
        return {"status": "error", "message": "查询扫码状态失败：返回不是 JSON"}

    if body.get("code") != 0:
        return {"status": "error", "message": "B 站返回 code=%s %s" % (body.get("code"), body.get("message"))}
    data = body.get("data") or {}
    code = int(data.get("code") or 0)
    if code != 0:
        state, msg = QR_STATE.get(code, ("error", "未知扫码状态 code=%s" % code))
        out = {"status": state, "message": data.get("message") or msg, "code": code}
        if state == "error":
            out["message"] = "扫码状态异常：%s" % out["message"]
        return out

    # 登录成功：优先用 Set-Cookie，缺了再从跨域登录链接里补
    sess = cookies.get("SESSDATA", "")
    if not sess:
        cookies = {**_cookies_from_crossdomain(data.get("url") or ""), **cookies}
        sess = cookies.get("SESSDATA", "")
    if not sess:
        return {"status": "error", "message": "扫码成功但没拿到 SESSDATA（B 站改了返回格式？）"}

    ok, uname, mid, msg = _verify(sess)
    if not ok:
        return {"status": "error", "message": "登录成功但校验不通过：%s" % msg}
    from collector.bili import mask_sessdata
    return {
        "status": "success",
        "message": "扫码登录成功，账号 %s（mid %s）" % (uname or "-", mid),
        "sessdata": sess,
        "uname": uname,
        "mid": mid,
        "masked": mask_sessdata(sess),
        # 发弹幕/送礼等写接口需要 bili_jct + DedeUserID，一并交出，由调用方决定存哪
        "cookies": cookies,
        "refresh_token": data.get("refresh_token") or "",
        "timestamp": data.get("timestamp") or 0,
    }


# ---------- 写配置 ----------
_SESS_LINE = re.compile(r"^(#\s*)?SESSDATA\s*=", re.I)


def write_env_sessdata(path: str | Path, sessdata: str | None, uname: str = "",
                       mid: int = 0, source: str = "") -> Path:
    """原子改写 bili.env 的 SESSDATA 行，保留文件里其他注释；权限收紧到 600。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = p.read_text("utf-8") if p.exists() else ""
    except OSError:
        text = ""
    src = {"qr": "扫码登录", "paste": "管理台粘贴"}.get(source, source or "手动")
    head = "# 由管理台于 %s 保存（%s；账号 %s mid=%s）" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), src, uname or "-", mid or 0)
    body = ("SESSDATA=%s" % sessdata) if sessdata else "# SESSDATA="
    out, replaced = [], False
    for ln in text.splitlines():
        if _SESS_LINE.match(ln.strip()):
            if not replaced:
                out.append(head)
                out.append(body)
                replaced = True
            continue
        out.append(ln)
    if not replaced:
        out.extend([head, body])
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", "utf-8")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p
