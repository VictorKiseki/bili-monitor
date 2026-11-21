"""出站地址白名单（S2，2026-11-14）。

有两类地址以前是「上游返回什么就用什么」：
  · 直播 FLV 直链：由 B 站 playurl 响应里的 host + base_url + extra 拼出来，
    拼完直接交给浏览器和本地转发；
  · 官方 QQ 机器人 gateway：/gateway 返回的 wss 地址，直接 websockets.connect。

任何一处上游响应被投毒（CDN 被打、中间人、测试环境改 hosts），本机就会去连
内网地址 —— 这是一条 SSRF 通道。判定统一放在这里：scheme 必须在允许集合内、
不许带 userinfo、hostname 必须是白名单后缀本身或以 ".后缀" 结尾；
**不做子串包含判断**，否则 api.qq.com.evil.com 也能混过去。
"""
from __future__ import annotations

from urllib.parse import urlsplit

# B 站自己的 CDN：http_stream 直链只会落在这几个域（与图片下载同一口径）
STREAM_SUFFIXES = (".bilivideo.com", ".bilivideo.cn", ".hdslb.com",
                   ".hdslb.cn", ".bilibili.com", ".szbdyd.com")
# 官方 QQ 机器人 WebSocket 网关（api.sgroup.qq.com / sandbox.api.sgroup.qq.com）
QQ_GATEWAY_SUFFIXES = (".qq.com",)


def host_matches(host: str, suffixes) -> bool:
    """host 是否是白名单后缀本身或以 ".后缀" 结尾（大小写与尾点归一）。"""
    text = str(host or "").strip().lower().rstrip(".")
    if not text:
        return False
    return any(text == s.lstrip(".") or text.endswith(s) for s in suffixes)


def url_allowed(url: str, suffixes, schemes=("http", "https")) -> bool:
    """url 是否落在「scheme 允许 + 有 host + 无 userinfo + 后缀白名单」内。"""
    try:
        u = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    if u.scheme.lower() not in schemes or u.username or u.password:
        return False
    return host_matches(u.hostname, suffixes)


def stream_url_ok(url: str) -> bool:
    """FLV 直链：只认 http(s) + B 站 CDN 域。"""
    return url_allowed(url, STREAM_SUFFIXES, schemes=("http", "https"))


def qq_gateway_ok(url: str) -> bool:
    """QQ 机器人 gateway：只认 wss + qq.com。"""
    return url_allowed(url, QQ_GATEWAY_SUFFIXES, schemes=("wss",))
