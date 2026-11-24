"""B-4. Dispatcher —— 调度骨架：claim → render → 限流 → send → retry/drop

职责边界（docs/05 第 6 节）：
  · 只对 SendStatus.RETRY 重试，最多 3 次（2s / 8s / 30s）
  · SendStatus.DROP 立即落库，不再重试，Web 台可见
  · (event_id, target_id) 由 Store 保证唯一，重复投递不会重发
2026-11-07（H3）：重试策略（次数 + 延迟表）的唯一实现在本类 plan_retry()，
Store 只负责把算好的 next_at / attempts 落库，不再自己判死信 —— 以前两边各存一份，
改了一边忘了另一边就会出现「已死信却当成功排期」。
未实现（明确留到后续）：同主播同类事件 60s 合并窗口（docs/02 §5.3 第 2 行）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Callable

from notifications.ratelimit import RateLimiter
from notifications.render import render
from shared.contracts import Event, EventType, RenderedMessage, SendResult, SendStatus, Target

log = logging.getLogger("dispatcher")


@dataclass
class Outcome:
    delivery_id: int
    event_id: str
    target_id: int
    action: str            # sent / retry_scheduled / dead_letter
    attempts: int
    error: str | None = None
    detail: str | None = None      # media / text_only：本次到底发的是卡片图还是纯文字


RAW_MAX_BYTES = 65536          # M11：死信原文上限（UTF-8 字节）

# H5（2026-11-14）：delivery.raw 里不许出现 QQ openid 明文（AGENTS §4）。
# target.external_id 就是 user_openid，以前 asdict 原样写进死信原文，控制台又回显前 4000 字符，
# 等于把本机唯一标识抄进了数据库与日志。这里按「键名归一化后以 openid 结尾或等于 external_id」
# 脱敏，保留末 4 位便于人工对上是哪个目标（target_id 本身是明文，够定位了）。
_ID_KEYS = ("external_id", "openid", "user_openid", "union_openid", "open_id")


def _mask_id(value: object) -> str:
    s = str(value or "")
    if not s:
        return ""
    if len(s) <= 8:
        return "****"
    return "****" + s[-4:]


def _mask_ids(obj):
    """递归把 openid 类字段脱敏；dict/list 原地改副本，其它类型原样返回。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and (k.strip().lower() in _ID_KEYS
                                       or k.strip().lower().endswith("openid")):
                out[k] = _mask_id(v)
            else:
                out[k] = _mask_ids(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_mask_ids(v) for v in obj]
    return obj


def _dump_raw(obj: dict) -> str:
    """把死信原文序列化成 JSON 字符串。

    JSON + ensure_ascii=False 保留中文；超过 65536 字节按 UTF-8 边界截断，
    不会切出半个汉字。asdict 只认 dataclass，extra 里的其它对象由 default=str 兜底。
    """
    try:
        text = json.dumps(_mask_ids(obj), ensure_ascii=False, default=str)
    except Exception:                     # noqa: BLE001 - 序列化失败也要把死信记下来
        text = repr(obj)
    data = text.encode("utf-8")
    if len(data) > RAW_MAX_BYTES:
        text = data[:RAW_MAX_BYTES].decode("utf-8", "ignore")
    return text


def _render_input_raw(ev: Event, target: Target) -> str:
    """M11：渲染失败的输入原文（event + target），随死信落库供复盘。"""
    return _dump_raw({"event": asdict(ev), "target": asdict(target)})


def _send_failure_raw(ev: Event, target: Target, res: SendResult) -> str:
    """死信原文，发送侧版：event + target 原文 + 本次请求摘要与平台回执。

    以前「发送失败 → 死信」只写一行 last_error（例如
    「HTTP 400 code=40054007 message=消息发送失败, 长度超长」），看不出当时到底发了
    多大、带没带图，长度类错误因此无法复盘。现在把通道返回的 raw（请求体摘要 +
    响应 body）一并落库；正文本身不进 raw，可由这里的 event 重新渲染出来。
    """
    return _dump_raw({
        "event": asdict(ev),
        "target": asdict(target),
        "send": {
            "status": getattr(res.status, "value", str(res.status)),
            "error_code": res.error_code,
            "error_msg": res.error_msg,
            "channel_raw": res.raw,
        },
    })


class Dispatcher:
    def __init__(
        self,
        store,
        channel,
        limiter: RateLimiter | None = None,
        batch: int = 20,
        now: Callable[[], float] = time.time,
        max_attempts: int = 3,
        retry_delays: tuple[float, ...] = (2.0, 8.0, 30.0),
        cardmaker=None,
        log: Callable[[str], None] | None = None,
    ):
        self.store = store
        self.channel = channel
        self.limiter = limiter or RateLimiter()
        self.batch = batch
        # H3：重试策略只在这里。Store 侧的同名参数已降级为「老脚本不带计划调用」的兜底。
        self.max_attempts = max_attempts
        self.retry_delays = tuple(retry_delays) or (2.0, 8.0, 30.0)
        self.cardmaker = cardmaker
        self._log = log            # 自定义按日落盘日志（不给就退回标准 logging）
        self._now = now
        self._cap = channel.capability()

    def plan_retry(self, attempts_after: int) -> float | None:
        """返回下次重试的绝对时间；None 表示重试次数已耗尽（该进死信）。"""
        if attempts_after >= self.max_attempts:
            return None
        idx = min(max(attempts_after, 1) - 1, len(self.retry_delays) - 1)
        return self._now() + self.retry_delays[idx]

    async def _settle(self, delivery, status: SendStatus, error: str | None, *,
                      attempts: int | None = None, next_at: float | None = None,
                      raw: str | None = None) -> bool:
        """H4：落发送结果，并把「已被回收重领、结果作废」这种情况记一行日志。

        Store 侧按领取令牌 claimed_at 做 fencing（H4）：返回 False 说明这条投递在发送期间
        被 reclaim_stale_claims() 打回、又被别的进程领走了。此时绝不能覆盖状态。
        """
        ok = await self.store.finish(delivery.delivery_id, status, error,
                                     attempts=attempts, next_at=next_at, raw=raw,
                                     claim_at=getattr(delivery, "claim_at", None))
        if ok is False:
            log.warning("投递结果丢弃 delivery=%s event=%s：该条已被回收并重新领取（%s）",
                        delivery.delivery_id, delivery.event_id,
                        getattr(status, "value", status))
        return bool(ok)

    async def _prepare(self, ev: Event) -> RenderedMessage:
        msg = render(ev, max_len=self._cap.max_text_len)
        image_path = None
        if self.cardmaker is not None and self._cap.supports_image:
            try:
                image_path = await self.cardmaker.build(ev)
            except Exception:                       # noqa: BLE001 - 画图失败不能挡住文字
                log.exception("卡片图生成异常，退回纯文字 %s", ev.event_id)
        text = msg.text
        # 没图时正文没地方显示（动态文案已按要求不带详情），补一行摘要避免只剩「发布了新动态」
        if image_path is None and ev.type in (EventType.DYNAMIC, EventType.VIDEO, EventType.ARTICLE):
            snippet = " ".join((ev.summary or "").split())
            if snippet and snippet not in text:
                text = text.rstrip() + "\n" + (snippet[:60] + ("…" if len(snippet) > 60 else ""))
        return RenderedMessage(
            text=text[: self._cap.max_text_len],
            image_url=msg.image_url if self._cap.supports_image else None,
            link_url=msg.link_url,
            image_path=image_path,
            extra=msg.extra,
        )

    async def _discard_card(self, ev: Event) -> None:
        """卡片图只在这条事件还没发完时有用：所有投递都到终态就删掉。

        多条投递（多目标）共用同一张卡，所以不能一发送成功就删；
        重试也还要用这张卡，所以只在终态（sent / 丢弃 / 重试耗尽）时才看计数。
        2026-11-07：open_deliveries 已改为统计 pending+sending，两阶段领取期间不会误删。
        """
        maker = self.cardmaker
        if maker is None or not hasattr(maker, "discard"):
            return
        counter = getattr(self.store, "open_deliveries", None)
        try:
            if counter is not None and await counter(ev.event_id):
                return
            if maker.discard(ev):
                msg = "已删除卡片缓存 event=%s（已推送完）" % ev.event_id
                if self._log is not None:
                    self._log("[缓存] " + msg)
                else:
                    log.info(msg)
        except Exception:                       # noqa: BLE001 - 清缓存失败不影响投递
            log.debug("清卡片缓存失败 %s", ev.event_id, exc_info=True)

    async def gc_media(self) -> dict:
        """兜底清缓存：主循环每半小时调一次，防止进程崩在「已发送」和「删文件」之间。"""
        maker = self.cardmaker
        if maker is None or not hasattr(maker, "gc"):
            return {}
        try:
            return await asyncio.to_thread(maker.gc)
        except Exception:                       # noqa: BLE001
            log.debug("缓存清理失败", exc_info=True)
            return {}

    async def _dispatch_enabled(self) -> bool:
        get_meta = getattr(self.store, "get_meta", None)
        return get_meta is None or await get_meta("global_mode", "full") == "full"

    async def deliver_one(self, delivery, ev: Event, target: Target) -> Outcome:
        try:
            msg = await self._prepare(ev)
        except ValueError as e:
            # M11：以前这里静默 DROP，日志里什么都没有，只能看到死信却不知道原因。
            # 现在把渲染输入原文（event + target）一并落库，死信可复盘；last_error 仍是短摘要。
            err = f"render_failed: {type(e).__name__}: {e}"[:300]
            raw = _render_input_raw(ev, target)
            log.warning("渲染失败，投递转死信 delivery=%s event=%s err=%s",
                        delivery.delivery_id, delivery.event_id, err)
            await self._settle(delivery, SendStatus.DROP, err,
                               attempts=delivery.attempts, raw=raw)
            return Outcome(delivery.delivery_id, delivery.event_id, delivery.target_id,
                           "dead_letter", delivery.attempts, err)

        await self.limiter.acquire(target.target_id)
        current_target = await self.store.get_target(target.target_id)
        if not await self._dispatch_enabled() or current_target is None or not current_target.enabled:
            await self._settle(delivery, SendStatus.RETRY, delivery.last_error,
                               attempts=delivery.attempts, next_at=delivery.next_at)
            return Outcome(delivery.delivery_id, delivery.event_id, delivery.target_id,
                           "paused", delivery.attempts, delivery.last_error)
        res: SendResult = await self.channel.send(target, msg)

        if res.status is SendStatus.OK:
            await self._settle(delivery, SendStatus.OK, None,
                               attempts=delivery.attempts)
            # 2026-10-31 补：成功也要记「带没带图」。之前只记 sent，出现「用户说没图、
            # 日志一切正常」时完全无从判断，只能靠事后重放猜。
            raw = res.raw or {}
            media = bool(raw.get("media"))
            note = raw.get("media_note")
            if media:
                log.info("已发送 delivery=%s event=%s 带卡片图", delivery.delivery_id, delivery.event_id)
            elif note:
                log.warning("已发送 delivery=%s event=%s 未带图片（纯文字）note=%s",
                            delivery.delivery_id, delivery.event_id, note)
            else:
                log.info("已发送 delivery=%s event=%s 纯文字", delivery.delivery_id, delivery.event_id)
            await self._discard_card(ev)
            return Outcome(delivery.delivery_id, delivery.event_id, delivery.target_id,
                           "sent", delivery.attempts, None, "media" if media else "text_only")

        if res.status is SendStatus.DROP:
            err = res.error_msg or res.error_code or "drop"
            # 2026-11-14：DROP 也要留原文，否则「首次发送即被拒」的死信只有一行错误码
            # （delivery 676 的 40054007 就是这样，raw=None 完全无法定位）
            raw = _send_failure_raw(ev, target, res)
            await self._settle(delivery, SendStatus.DROP, err,
                               attempts=delivery.attempts, raw=raw)
            log.warning("投递丢弃 delivery=%s err=%s", delivery.delivery_id, err)
            await self._discard_card(ev)
            return Outcome(delivery.delivery_id, delivery.event_id, delivery.target_id,
                           "dead_letter", delivery.attempts, err)

        err = res.error_msg or res.error_code or "retry"
        attempts_after = delivery.attempts + 1
        retry_at = self.plan_retry(attempts_after)
        if retry_at is None:
            # 重试耗尽：直接落终态（不带 next_at），Store 不再自行判定
            await self._settle(delivery, SendStatus.DROP, err,
                               attempts=attempts_after,
                               raw=_send_failure_raw(ev, target, res))
            log.error("重试耗尽 delivery=%s attempts=%s err=%s",
                      delivery.delivery_id, attempts_after, err)
            await self._discard_card(ev)
            return Outcome(delivery.delivery_id, delivery.event_id, delivery.target_id,
                           "dead_letter", attempts_after, err)
        await self._settle(delivery, SendStatus.RETRY, err,
                           attempts=attempts_after, next_at=retry_at)
        log.info("重试排期 delivery=%s attempts=%s next_at=%.1f err=%s",
                 delivery.delivery_id, attempts_after, retry_at, err)
        return Outcome(delivery.delivery_id, delivery.event_id, delivery.target_id,
                       "retry_scheduled", attempts_after, err)

    async def _load_batch(self, due: list):
        """M4：一次批量取回本轮要用的 Event / Target，替代逐条 get_event + get_target。"""
        event_ids = list({d.event_id for d in due})
        target_ids = list({d.target_id for d in due})
        get_events = getattr(self.store, "get_events", None)
        get_targets = getattr(self.store, "get_targets", None)
        if get_events is not None and get_targets is not None:
            return await get_events(event_ids), await get_targets(target_ids)
        # 兼容只实现单条接口的 Store：逐条兜底，行为与改动前一致
        events = {}
        for eid in event_ids:
            ev = await self.store.get_event(eid)
            if ev is not None:
                events[eid] = ev
        targets = {}
        for tid in target_ids:
            t = await self.store.get_target(tid)
            if t is not None:
                targets[tid] = t
        return events, targets

    async def tick(self) -> list[Outcome]:
        if not await self._dispatch_enabled():
            return []
        due = await self.store.claim_due(self.batch)
        if not due:
            return []
        events, targets = await self._load_batch(due)
        out: list[Outcome] = []
        for d in due:
            ev = events.get(d.event_id)
            target = targets.get(d.target_id)
            if ev is None or target is None:
                await self._settle(d, SendStatus.DROP, "missing_event_or_target",
                                   attempts=d.attempts)
                out.append(Outcome(d.delivery_id, d.event_id, d.target_id, "dead_letter",
                                   d.attempts, "missing_event_or_target"))
                continue
            out.append(await self.deliver_one(d, ev, target))
        return out
