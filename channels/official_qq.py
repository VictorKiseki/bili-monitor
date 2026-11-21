"""B-3. OfficialQQChannel —— 官方 QQ 机器人通道（M1 只用私聊）

凭据只从环境变量/参数来，不写进任何配置文件与日志；对外只暴露脱敏值。
接口事实见 docs/04：POST https://bots.qq.com/app/getAppAccessToken，
发消息 POST /v2/users/{user_openid}/messages 与 /v2/groups/{group_openid}/messages。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from shared.contracts import (
    ChannelCapability, ChannelType, RenderedMessage, SendResult, SendStatus, Target, TargetKind,
)

log = logging.getLogger("channel.qq")

# 地址与 intents 由 channels/qq_api.py 统一给出（审查细节 10），这里保留旧名字。
from channels.qq_api import API, TOKEN_URL  # noqa: E402,F401

# 一次性错误的映射，其余按 HTTP 状态码兜底
DROP_CODES = {
    "304023", "304024", "304025",      # openid / 内容相关不可恢复
    "40034", "40035", "40054",         # 参数 / 无权限
    "11244", "11245",                  # 机器人被踢出 / 目标不存在
}

# 40054007 = 消息长度超限（官方文档：「请缩短消息内容」）。以前它落到 _classify 的
# 「status in (400,403,404) → DROP」里，首次发送就判死信；但 2026-11-06 的 delivery 676
# 正文只有 77 字（127 字节）带卡片图照样被拒，说明长度不只由 content 决定，富媒体的
# file_info 与整个请求体也算在内。平台没公布具体上限，所以这里不猜数字：先退掉富媒体
# 重发纯文字，再按比例压正文，直到平台接受或降级次数用完（docs/10 §3.3）。
LENGTH_CODE = "40054007"
MIN_TEXT_CHARS = 40            # 压正文的地板：再短连标题与链接都留不住
MAX_DEGRADE_STEPS = 3          # 最多降级 3 次（去图 → 半长 → 再半长）


def _clip_text(text: str, limit: int) -> str:
    """把正文压到 limit 字符内，优先保住首行（标题）与末行（链接）。"""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    lines = [ln for ln in text.splitlines() if ln.strip()]
    head = lines[0] if lines else text
    tail = lines[-1] if len(lines) > 1 else ""
    if tail and len(head) + len(tail) + 3 <= limit:
        return head + "\n…\n" + tail
    if tail and len(tail) + 2 <= limit:
        # 连标题都放不下时宁可只留链接：链接是这条推送唯一可点的东西
        return "…\n" + tail
    return text[: limit - 1] + "…"


class OfficialQQChannel:
    name = ChannelType.OFFICIAL_QQ

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        timeout: float = 15.0,
        max_text_len: int = 900,
        upload_timeout: float = 40.0,
    ):
        if not app_id or not app_secret:
            raise ValueError("缺少 AppID/AppSecret")
        self._app_id = app_id
        self._secret = app_secret
        self._timeout = timeout
        # 传图是 base64 上行（600~900 KB），比发文字慢得多，必须单独给更长的超时，
        # 否则偶发一次 15s 超时就会静默退化成纯文字（2026-10-31 现象）。
        self._upload_timeout = upload_timeout
        self._info_cache: dict[tuple, tuple[str, float]] = {}
        self._http: httpx.AsyncClient | None = None
        self._http_up: httpx.AsyncClient | None = None
        self._token: str = ""
        self._token_exp: float = 0.0
        self._lock = asyncio.Lock()
        self._cap = ChannelCapability(
            max_text_len=max_text_len, supports_image=True,
            supports_markdown=False, min_interval_s=0.3,
        )
        self.last_error: str | None = None

    def capability(self) -> ChannelCapability:
        return self._cap

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)
        return self._http

    def _upload_client(self) -> httpx.AsyncClient:
        if self._http_up is None:
            self._http_up = httpx.AsyncClient(timeout=self._upload_timeout)
        return self._http_up

    async def _fetch_token(self) -> tuple[str, float]:
        r = await self._client().post(TOKEN_URL, json={"appId": self._app_id, "clientSecret": self._secret})
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"鉴权返回非 JSON，HTTP {r.status_code}")
        if r.status_code != 200 or not data.get("access_token"):
            raise RuntimeError(f"鉴权失败 code={data.get('code')} message={data.get('message')}")
        ttl = float(data.get("expires_in") or 7200)
        return data["access_token"], time.time() + ttl - 60     # 提前 60s 刷新

    async def token(self, force: bool = False) -> str:
        async with self._lock:
            if not force and self._token and time.time() < self._token_exp:
                return self._token
            self._token, self._token_exp = await self._fetch_token()
            return self._token

    async def health(self) -> bool:
        try:
            await self.token(force=True)
            return True
        except Exception as e:                       # noqa: BLE001 - 健康检查吞掉异常
            self.last_error = str(e)
            return False

    def _path(self, target: Target) -> str:
        if target.kind is TargetKind.PRIVATE:
            return f"/v2/users/{target.external_id}/messages"
        return f"/v2/groups/{target.external_id}/messages"

    @staticmethod
    def _classify(status: int, code: str | None, msg: str | None) -> SendStatus:
        if 200 <= status < 300:
            return SendStatus.OK
        if code and code in DROP_CODES:
            return SendStatus.DROP
        if status in (400, 403, 404):
            return SendStatus.DROP        # 权限/目标无效：重试没用
        if status == 429:
            return SendStatus.RETRY
        if status >= 500:
            return SendStatus.RETRY
        return SendStatus.RETRY

    async def _file_info(self, target: Target, path: str) -> str | None:
        """上传图片拿 file_info（srv_send_msg=False），供富媒体消息的 media.file_info 用。

        2026-10-31 修正：早先直接往发消息接口塞 media 不生效，根因是上传响应里的
        file_uuid 不是发消息要的 file_info，且二进制直传必须带 file_name。按官方
        文档补齐（上传拿 file_info → msg_type=7 带 media.file_info）后，图片可以和
        文字合并在同一条消息里下发，不再需要发两条。
        file_info 有 ttl，这里按 目标+文件+mtime 缓存，重试时直接复用。
        """
        p = Path(path)
        if not p.exists():
            self.last_error = "图片不存在：%s" % path
            return None
        try:
            st = p.stat()
        except OSError as e:
            self.last_error = "读图片失败：%s" % e
            return None
        key = (target.external_id, str(p), st.st_mtime_ns)
        hit = self._info_cache.get(key)
        if hit and hit[1] > time.time():
            return hit[0]
        raw_bytes = p.read_bytes()
        data = base64.b64encode(raw_bytes).decode("ascii")
        kind = "users" if target.kind is TargetKind.PRIVATE else "groups"
        url = "%s/v2/%s/%s/files" % (API, kind, target.external_id)
        for attempt in (1, 2):
            t0 = time.time()
            try:
                tok = await self.token(force=(attempt == 2))
                r = await self._upload_client().post(
                    url,
                    headers={
                        "Authorization": "QQBot " + tok,
                        "X-Union-Appid": self._app_id,
                        "Content-Type": "application/json",
                    },
                    json={"file_type": 1, "file_data": data, "file_name": p.name,
                          "srv_send_msg": False},
                )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                self.last_error = "图片上传网络错误 %s: %s" % (type(e).__name__, e)
                log.warning("图片上传网络错误 attempt=%s %s: %s", attempt, type(e).__name__, e)
                if attempt == 1:
                    await asyncio.sleep(1.0)
                continue
            try:
                body = r.json()
            except Exception:
                body = {}
            if r.status_code == 200 and body.get("file_info"):
                ttl = float(body.get("ttl") or 0)
                alive = (ttl if ttl > 0 else 600.0) - 30.0
                self._info_cache[key] = (body["file_info"], time.time() + max(alive, 30.0))
                log.info("图片上传成功 %d KB %.2fs ttl=%s",
                         len(raw_bytes) // 1024, time.time() - t0, ttl)
                return body["file_info"]
            if r.status_code == 401 and attempt == 1:
                continue
            self.last_error = "图片上传失败 HTTP %s code=%s message=%s" % (
                r.status_code, body.get("code"), body.get("message"))
            return None
        return None

    @staticmethod
    def _payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
        """请求体摘要：只记长度不记正文 —— 原文可由死信 raw 里的 event 重渲染出来，
        而长度正是 40054007 这类错误唯一需要知道的数字（旧死信只有一行 last_error，
        看不出当时到底发的多大）。"""
        content = payload.get("content") or ""
        media = payload.get("media") or {}
        info = media.get("file_info") or ""
        try:
            body_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        except Exception:                       # noqa: BLE001 - 摘要算不出来也不能挡住发送
            body_bytes = 0
        return {
            "msg_type": payload.get("msg_type"),
            "content_chars": len(content),
            "content_bytes": len(content.encode("utf-8")),
            "file_info_chars": len(info) if isinstance(info, str) else 0,
            "body_bytes": body_bytes,
            "reply": bool(payload.get("msg_id")),
        }

    @classmethod
    def _brief(cls, payload: dict[str, Any]) -> str:
        s = cls._payload_summary(payload)
        return ("msg_type=%s 正文%d字/%d字节 file_info=%d字 请求体%d字节"
                % (s["msg_type"], s["content_chars"], s["content_bytes"],
                   s["file_info_chars"], s["body_bytes"]))

    @staticmethod
    def _degrade(payload: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
        """按平台回执降级一次：先去掉富媒体，再把正文压一半。

        返回 (新 payload, 降级原因)；已经没有可降的内容时返回 None。
        """
        if payload.get("msg_type") == 7:
            plain: dict[str, Any] = {"content": payload.get("content", ""), "msg_type": 0}
            for key in ("msg_id", "msg_seq"):
                if key in payload:
                    plain[key] = payload[key]
            return plain, "no_media"
        text = payload.get("content") or ""
        if len(text) <= MIN_TEXT_CHARS:
            return None
        shorter = _clip_text(text, max(MIN_TEXT_CHARS, len(text) // 2))
        if shorter == text:
            return None
        return {**payload, "content": shorter}, "clip=%d" % len(shorter)

    async def _post(self, target: Target, payload: dict[str, Any]) -> SendResult:
        """发一次消息（401 时强制刷 token 重打一次），请求摘要与响应回执都放进 raw。"""
        for attempt in (1, 2):
            try:
                tok = await self.token(force=(attempt == 2))
                r = await self._client().post(
                    API + self._path(target),
                    headers={
                        "Authorization": "QQBot " + tok,
                        "X-Union-Appid": self._app_id,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                self.last_error = f"{type(e).__name__}: {e}"
                return SendResult(status=SendStatus.RETRY, error_code="network",
                                  error_msg=self.last_error,
                                  raw={"request": self._payload_summary(payload)})

            try:
                body = r.json()
            except Exception:
                body = {}
            code = str(body.get("code") or "") or None
            message = body.get("message")
            status = self._classify(r.status_code, code, message)

            # 401 通常是 token 过期，强制刷新后重打一次
            if r.status_code == 401 and attempt == 1:
                continue
            request = self._payload_summary(payload)
            if status is SendStatus.OK:
                self.last_error = None
                return SendResult(status=SendStatus.OK, raw={
                    "id": body.get("id") or body.get("message_id"),
                    "http": r.status_code,
                    "media": payload.get("msg_type") == 7,
                    "request": request,
                })
            self.last_error = f"HTTP {r.status_code} code={code} message={message}"
            return SendResult(status=status, error_code=code or str(r.status_code),
                              error_msg=self.last_error,
                              raw={"body": body, "http": r.status_code, "request": request})

        return SendResult(status=SendStatus.DROP, error_code="401", error_msg="token 刷新后仍鉴权失败")

    async def send(self, target: Target, msg: RenderedMessage) -> SendResult:
        text = (msg.text or "").strip()[: self._cap.max_text_len]
        if not text:
            return SendResult(status=SendStatus.DROP, error_code="empty", error_msg="消息正文为空")

        # 图片合并进同一条富媒体消息（msg_type=7 + media.file_info）：
        # 上传失败就退回纯文字，宁可少一张图也不要整条推送失败。
        payload: dict[str, Any] = {"content": text, "msg_type": 0}
        media_note = None
        if msg.image_path:
            info = await self._file_info(target, msg.image_path)
            if info:
                payload = {"content": text, "msg_type": 7, "media": {"file_info": info}}
            else:
                media_note = self.last_error
                log.warning("图片上传失败，本条退回纯文字：%s", media_note)
        if msg.extra.get("reply_to"):
            payload["msg_id"] = str(msg.extra["reply_to"])
            payload["msg_seq"] = int(time.time()) % 100000

        first_request = self._payload_summary(payload)
        res = await self._post(target, payload)

        # 40054007 长度超限：不判死、也不猜平台上限，按回执逐级降级重发。
        degraded: list[str] = []
        while (res.status is SendStatus.DROP
               and res.error_code == LENGTH_CODE
               and len(degraded) < MAX_DEGRADE_STEPS):
            step = self._degrade(payload)
            if step is None:
                break
            payload, why = step
            degraded.append(why)
            if why == "no_media":
                note = "长度超限（40054007），本条改为纯文字"
            else:
                note = "长度超限（40054007），正文压到 %d 字" % len(payload.get("content") or "")
            # 上传失败也会先写一条 note：两种降级撞在一起时不能把前一条盖掉
            media_note = note if media_note is None else media_note + "；" + note
            log.warning("消息长度超限（40054007）→ 降级重发 %s：%s", why, self._brief(payload))
            res = await self._post(target, payload)

        if res.status is SendStatus.OK or degraded:
            raw = dict(res.raw or {})
            raw["media"] = payload.get("msg_type") == 7
            if media_note is not None:
                raw["media_note"] = media_note
            if degraded:
                raw["degraded"] = list(degraded)
                # 降级后 raw["request"] 只剩最后一次的摘要，原始那一版也要留：
                # 「原本多长、最后压到多长」两个数字合起来才能判断是不是真超长。
                raw["original_request"] = first_request
            res = SendResult(status=res.status, error_code=res.error_code,
                             error_msg=res.error_msg, raw=raw)
        return res

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._http_up is not None:
            await self._http_up.aclose()
            self._http_up = None
