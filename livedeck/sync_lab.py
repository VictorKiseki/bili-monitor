"""同步测试页的独立 HTTP-FLV 通道。"""
from __future__ import annotations

import threading

import httpx

from livedeck.app import DeckError

_slots = threading.BoundedSemaphore(3)


def serve_stream(handler, query: dict) -> None:
    if handler.headers.get("Sec-Fetch-Site") not in (None, "same-origin"):
        return handler._err("同步测试流只接受本页请求", 403)
    value = (query.get("room") or [""])[0]
    if not (value.isascii() and value.isdecimal() and len(value) <= 12 and int(value)):
        return handler._err("房间号无效")
    if not _slots.acquire(timeout=2.0):
        return handler._err("同步测试最多同时拉三路，请先停止其他测试", 429)
    try:
        deck = handler.deck
        # 复用已校验直播源域名的接口，避免写入舞台的房间、画质和播放地址缓存。
        url, _, _, _ = deck._playinfo(int(value), 250)
        client = deck.stream_http
        with client.stream("GET", url) as upstream:
            if upstream.status_code != 200:
                return handler._err("同步测试直播流不可用", 502)
            handler.close_connection = True
            handler.connection.settimeout(5.0)
            handler.send_response(200)
            handler.send_header("Content-Type", "video/x-flv")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Cross-Origin-Resource-Policy", "same-origin")
            handler.send_header("Connection", "close")
            try:
                handler.end_headers()
                for chunk in upstream.iter_bytes(16 * 1024):
                    handler.wfile.write(chunk)
            except (OSError, httpx.HTTPError):
                # 停止、关页或上游读超时时，由上下文管理器关掉 CDN 连接。
                pass
    except DeckError as error:
        handler._err("该房间暂无可用直播流" if error.code == 404 else "获取同步测试直播流失败", error.code)
    except httpx.HTTPError:
        handler._err("连接同步测试直播流失败", 502)
    finally:
        _slots.release()
