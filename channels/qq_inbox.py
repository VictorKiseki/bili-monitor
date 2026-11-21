"""官方 QQ 机器人入站：常驻 WebSocket 收私聊消息，交给清理确认命令。

只处理 C2C_MESSAGE_CREATE，且只接受配置的 owner user_openid，避免陌生人改清理计划。
出站仍复用 OfficialQQChannel（run_bot 传 send 回调），本模块不重复发消息逻辑。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Awaitable, Callable

import httpx

from shared.contracts import RenderedMessage, SendStatus
from shared.netsafe import qq_gateway_ok

from channels.qq_api import API, INTENTS, TOKEN_URL  # noqa: E402,F401  （常量统一放 qq_api）


class QQInbox:
    def __init__(
        self,
        app_id: str,
        secret: str,
        owner_openid: str,
        on_message: Callable[[str], str | None],
        send: Callable[[str], Awaitable[object]],
        log: Callable[[str], None] | None = None,
        timeout: float = 20.0,
        paused: Callable[[], bool] | None = None,
        on_beat: Callable[[], None] | None = None,
    ) -> None:
        self._app_id = app_id
        self._secret = secret
        self._owner = owner_openid
        self._on_message = on_message
        self._send = send
        self._log = log or (lambda _m: None)
        self._timeout = timeout
        # 管理台「进程」页的开关：paused 为真时连接照常保持心跳，但不执行任何命令
        self._paused = paused
        self._on_beat = on_beat
        self._stop = asyncio.Event()
        self._token = ""
        self._token_exp = 0.0
        self._http: httpx.AsyncClient | None = None
        self.last_error = ""

    def _beat(self) -> None:
        if self._on_beat is not None:
            try:
                self._on_beat()
            except Exception:  # noqa: BLE001 心跳失败不能影响收消息
                pass

    async def stop(self) -> None:
        self._stop.set()
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)
        return self._http

    async def _get_token(self, force: bool = False) -> str:
        if not force and self._token and time.time() < self._token_exp:
            return self._token
        r = await self._client().post(TOKEN_URL, json={"appId": self._app_id, "clientSecret": self._secret})
        data = r.json()
        if r.status_code != 200 or not data.get("access_token"):
            raise RuntimeError("取 token 失败 HTTP %s: %s" % (r.status_code, r.text[:200]))
        self._token = str(data["access_token"])
        self._token_exp = time.time() + float(data.get("expires_in") or 7200) - 60
        return self._token

    async def _gateway(self) -> str:
        token = await self._get_token()
        r = await self._client().get(API + "/gateway", headers={"Authorization": "QQBot " + token})
        try:
            data = r.json()
        except Exception:
            data = {}
        url = data.get("url")
        if not url:
            raise RuntimeError("取 gateway 失败 HTTP %s: %s" % (r.status_code, r.text[:200]))
        if not qq_gateway_ok(str(url)):
            # S2：地址来自 /gateway 响应，直接 websockets.connect 等于把入站通道
            # 交给上游的返回值。只认 wss://*.qq.com，其余拒绝连接。
            raise RuntimeError("gateway 地址不在 qq.com 白名单内，已拒绝连接：%s"
                               % str(url)[:120])
        return str(url)

    async def run(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                await self._session()
                backoff = 5.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = "%s: %s" % (type(e).__name__, e)
                self._log("[QQ收件] 连接断开：%s；%.0fs 后重连" % (self.last_error, backoff))
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(60.0, backoff * 1.8)
        self._log("[QQ收件] 已停止")

    async def _session(self) -> None:
        import websockets
        from websockets.exceptions import ConnectionClosed

        token = await self._get_token()
        url = await self._gateway()
        self._log("[QQ收件] 连接 gateway")
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            hello = json.loads(await ws.recv())
            beat = float((hello.get("d") or {}).get("heartbeat_interval", 30000)) / 1000.0
            await ws.send(json.dumps({
                "op": 2,
                "d": {"token": "QQBot " + token, "intents": INTENTS,
                      "shard": [0, 1], "properties": {}},
            }))
            seq = None
            last_beat = time.monotonic()
            while not self._stop.is_set():
                if time.monotonic() - last_beat >= beat:
                    await ws.send(json.dumps({"op": 1, "d": seq}))
                    last_beat = time.monotonic()
                    self._beat()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except ConnectionClosed:
                    return
                ev = json.loads(raw)
                op, t = ev.get("op"), ev.get("t")
                if ev.get("s") is not None:
                    seq = ev["s"]
                if op == 7:  # 平台要求重连
                    return
                if op == 9:  # invalid session：token 可能失效，强制刷新
                    self._token = ""
                    self._token_exp = 0.0
                    return
                if op != 0:
                    continue
                d = ev.get("d") or {}
                if t == "READY":
                    u = d.get("user") or {}
                    self._log("[QQ收件] READY %s" % (u.get("username") or u.get("id") or ""))
                    self._beat()
                    continue
                if t != "C2C_MESSAGE_CREATE":
                    continue
                uid = str((d.get("author") or {}).get("user_openid") or "")
                if not uid or uid != self._owner:
                    continue
                self._beat()
                if self._paused is not None and self._paused():
                    # 暂停期间不执行命令（清理确认之类），避免用户在关掉之后仍被改计划
                    self._log("[QQ收件] 已暂停（管理台「进程」页），忽略一条私聊命令")
                    continue
                content = str(d.get("content") or "").strip()
                if not content:
                    continue
                try:
                    reply = self._on_message(content)
                except Exception as e:  # noqa: BLE001
                    self.last_error = "处理命令失败：%s" % e
                    reply = "处理失败：%s" % e
                if reply:
                    res = await self._send(reply)
                    if getattr(res, "status", None) is not SendStatus.OK:
                        self._log("[QQ收件] 回复发送失败：%s"
                                  % getattr(res, "error_msg", getattr(res, "status", "")))
