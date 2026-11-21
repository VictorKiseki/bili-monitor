"""B-2. MemoryStore —— 契约 Store 的零依赖实现（联调 / 自测用）

重试调度约定（H3，2026-11-07 起）：重试策略的唯一来源是 Dispatcher —— 它把
next_at / attempts 算好后交给 finish() 落库。本类只保留 retry_delays/max_attempts
作为「调用方没给计划」时的兜底（老脚本直接 finish(RETRY)），不再自行判定死信。
与 SQLiteStore 同名同义。
"""
from __future__ import annotations

import time
from dataclasses import replace

from shared.contracts import (
    ChannelType, Delivery, Event, SendStatus, Subject, SubjectState, Target, TargetKind,
)

# M11：死信列表 raw 预览上限（字符），与 SQLiteStore.RAW_PREVIEW_CHARS 保持一致
RAW_PREVIEW_CHARS = 4000


class MemoryStore:
    def __init__(self, now=None, retry_delays=(2.0, 8.0, 30.0), max_attempts: int = 3):
        self._now = now or time.time
        # 同上：仅为兼容老调用方保留的兜底策略，生产路径由 Dispatcher 显式给计划
        self.retry_delays = tuple(retry_delays)
        self.max_attempts = max_attempts
        self.subjects: dict[int, Subject] = {}
        self.states: dict[int, SubjectState] = {}
        self.targets: dict[int, Target] = {}
        self.events: dict[str, Event] = {}
        self._inserted: set[str] = set()
        self._deliveries: dict[int, dict] = {}
        self._next_delivery_id = 1
        self._enqueued: set[tuple[str, int]] = set()
        # H1：id_str -> pub_ts（时间窗去重需要发布时间）
        self._seen: dict[int, dict[str, int]] = {}
        self._meta: dict[str, str] = {}
        self._sessions: dict[int, dict] = {}

    # ---------- 基础 ----------
    async def list_subjects(self, enabled_only: bool = True) -> list[Subject]:
        vals = list(self.subjects.values())
        return [s for s in vals if s.enabled] if enabled_only else vals

    async def get_subject(self, uid: int) -> Subject | None:
        return self.subjects.get(uid)

    async def save_subject_state(self, state: SubjectState) -> None:
        self.states[state.uid] = state

    async def get_subject_state(self, uid: int) -> SubjectState | None:
        return self.states.get(uid)

    async def add_target(self, t: Target) -> None:
        self.targets[t.target_id] = t

    async def get_target(self, target_id: int) -> Target | None:
        return self.targets.get(target_id)

    async def get_targets(self, target_ids: list[int]) -> dict[int, Target]:
        """M4：批量取目标，避免 dispatcher 逐条 N+1。"""
        return {tid: self.targets[tid] for tid in target_ids if tid in self.targets}

    # ---------- 事件（幂等） ----------
    async def insert_event(self, ev: Event) -> bool:
        if ev.event_id in self._inserted:
            return False
        self._inserted.add(ev.event_id)
        self.events[ev.event_id] = ev
        return True

    async def get_event(self, event_id: str) -> Event | None:
        return self.events.get(event_id)

    async def get_events(self, event_ids: list[str]) -> dict[str, Event]:
        """M4：批量取事件。"""
        return {eid: self.events[eid] for eid in event_ids if eid in self.events}

    # ---------- 投递 ----------
    async def enqueue(self, event_id: str, target_ids: list[int]) -> int:
        n = 0
        for tid in target_ids:
            key = (event_id, tid)
            if key in self._enqueued:
                continue          # (event_id, target_id) 唯一索引
            self._enqueued.add(key)
            did = self._next_delivery_id
            self._next_delivery_id += 1
            self._deliveries[did] = {
                "delivery_id": did, "event_id": event_id, "target_id": tid,
                "attempts": 0, "next_at": 0.0, "status": "pending",
                "last_error": None, "claimed_at": None, "raw": None,
            }
            n += 1
        return n

    async def claim_due(self, limit: int = 20) -> list[Delivery]:
        """H2 两阶段领取：pending -> sending，claimed_at 记领取时刻。"""
        now = self._now()
        out: list[Delivery] = []
        for d in sorted(self._deliveries.values(), key=lambda x: (x["next_at"], x["delivery_id"])):
            if len(out) >= limit:
                break
            if d["status"] != "pending" or d["next_at"] > now:
                continue
            d["status"] = "sending"
            d["claimed_at"] = now
            out.append(Delivery(
                delivery_id=d["delivery_id"], event_id=d["event_id"], target_id=d["target_id"],
                attempts=d["attempts"], next_at=d["next_at"], status=d["status"],
                last_error=d["last_error"], claim_at=now,
            ))
        return out

    async def finish(self, delivery_id: int, status: SendStatus, error: str | None,
                     *, attempts: int | None = None, next_at: float | None = None,
                     raw: str | None = None, claim_at: float | None = None) -> bool:
        """H3：只执行 Dispatcher 算好的结果；两者都没给时按旧策略兜底。

        M11：raw 只在显式给出时写入，且保留第一次写入的值（不覆盖已有原文）。

        H4（2026-11-14）：与 SQLiteStore 同义 —— 给了 claim_at 就按领取令牌做 fencing，
        令牌对不上（已被回收重领）说明这是迟到结果，直接丢弃并返回 False。
        """
        d = self._deliveries[delivery_id]
        if claim_at is not None and (d["status"] != "sending" or d.get("claimed_at") != claim_at):
            return False
        d["claimed_at"] = None
        d["last_error"] = error
        if raw is not None and not d.get("raw"):
            d["raw"] = raw
        if status is SendStatus.OK:
            d["status"] = "sent"
            if attempts is not None:
                d["attempts"] = attempts
            return True
        if status is SendStatus.RETRY and next_at is not None:
            d["status"] = "pending"
            d["attempts"] = attempts if attempts is not None else d["attempts"]
            d["next_at"] = next_at
            return True
        if status is SendStatus.RETRY:
            after = attempts if attempts is not None else d["attempts"] + 1
            d["attempts"] = after
            if after >= self.max_attempts:
                d["status"] = "failed"     # 死信：重试耗尽
                return True
            idx = min(after - 1, len(self.retry_delays) - 1)
            d["next_at"] = self._now() + self.retry_delays[idx]
            return True
        # DROP：不可重试，直接死信；attempts 以调用方给的为准（它算的是这一轮之后的次数）
        if attempts is not None:
            d["attempts"] = attempts
        d["status"] = "failed"
        return True

    async def reclaim_stale_claims(self, older_than_s: int = 300) -> int:
        """H2：把超时仍在 sending 的投递捞回 pending。"""
        now = self._now()
        cutoff = now - max(0, int(older_than_s))
        n = 0
        for d in self._deliveries.values():
            if d["status"] != "sending":
                continue
            claimed_at = d.get("claimed_at")
            if claimed_at is not None and claimed_at >= cutoff:
                continue
            d["status"] = "pending"
            d["next_at"] = now
            d["claimed_at"] = None
            n += 1
        return n

    async def prune(self, *, seen_days: int = 90, event_days: int = 180) -> dict[str, int]:
        """M12：清理过期去重水位/已终结投递/过期事件；pending、sending 永不删。"""
        now = int(self._now())
        seen_cutoff = now - max(0, seen_days) * 86400
        event_cutoff = now - max(0, event_days) * 86400
        out = {"seen_dynamic": 0, "delivery": 0, "event": 0}
        for uid, bucket in list(self._seen.items()):
            for id_str in [k for k, ts in bucket.items() if ts < seen_cutoff]:
                del bucket[id_str]
                out["seen_dynamic"] += 1
            if not bucket:
                self._seen.pop(uid, None)
        old_events = {eid for eid, ev in self.events.items() if ev.occurred_at < event_cutoff}
        for did, d in list(self._deliveries.items()):
            if d["status"] in ("sent", "failed", "dismissed") and (
                    d["event_id"] not in self.events or d["event_id"] in old_events):
                del self._deliveries[did]
                self._enqueued.discard((d["event_id"], d["target_id"]))
                out["delivery"] += 1
        # H7（2026-11-14）：还挂着 pending/sending 投递的过期事件先留着，
        # 与 SQLiteStore（ON DELETE CASCADE 会连投递一起删）保持同一口径。
        busy = {d["event_id"] for d in self._deliveries.values()
                if d["status"] in ("pending", "sending")}
        for eid in old_events - busy:
            self.events.pop(eid, None)
            self._inserted.discard(eid)
            out["event"] += 1
        return out

    async def open_deliveries(self, event_id: str) -> int:
        # 两阶段领取后「没到终态」= pending + sending
        return sum(1 for d in self._deliveries.values()
                   if d["event_id"] == event_id and d["status"] in ("pending", "sending"))

    # ---------- 观测 ----------
    def snapshot(self) -> dict:
        stats = {"pending": 0, "sent": 0, "failed": 0}
        for d in self._deliveries.values():
            stats[d["status"]] = stats.get(d["status"], 0) + 1
        return {"stats": stats, "deliveries": len(self._deliveries)}

    def dead_letters(self) -> list[dict]:
        out = []
        for d in self._deliveries.values():
            if d["status"] != "failed":
                continue
            # 与 SQLiteStore 查询侧一致：raw 预览 + has_raw，既有键不动（M11）
            item = dict(d)
            raw = item.get("raw")
            item["has_raw"] = bool(raw)
            if raw and len(raw) > RAW_PREVIEW_CHARS:
                item["raw"] = raw[:RAW_PREVIEW_CHARS]
            out.append(item)
        return out


    # ---------- 主循环辅助（与 SQLiteStore 同名同义） ----------
    async def upsert_subject(self, s: Subject) -> None:
        # 与 SQLite 的 ON CONFLICT 分支保持一致：已存在的行只更新名字/房间，不碰 enabled
        old = self.subjects.get(s.uid)
        self.subjects[s.uid] = replace(s, enabled=old.enabled) if old is not None else s

    async def set_subject_enabled(self, uid: int, enabled: bool) -> bool:
        s = self.subjects.get(uid)
        if s is None:
            return False
        self.subjects[uid] = replace(s, enabled=bool(enabled))
        return True

    async def all_states(self) -> dict[int, SubjectState]:
        return dict(self.states)

    async def enabled_target_ids(self) -> list[int]:
        return [t.target_id for t in self.targets.values() if t.enabled]

    async def seen_ids(self, uid: int, since_ts: int | None = None) -> set[str]:
        """H1：since_ts 非空时只返回 pub_ts>=since_ts 的 id；None 为旧的全量行为。"""
        bucket = self._seen.get(uid, {})
        if since_ts is None:
            return set(bucket)
        return {id_str for id_str, ts in bucket.items() if ts >= int(since_ts)}

    async def mark_seen(self, uid: int, items: list[tuple[str, int]]) -> None:
        bucket = self._seen.setdefault(uid, {})
        for id_str, ts in items:
            bucket[id_str] = int(ts)

    async def upsert_live_session(self, uid: int, start_ts: int, online: int, title: str = "") -> None:
        cur = self._sessions.get(uid) or {"start_ts": 0, "online_max": 0, "title": ""}
        cur["start_ts"] = cur["start_ts"] or int(start_ts)
        cur["online_max"] = max(int(cur["online_max"]), int(online))
        cur["title"] = title or cur["title"]
        self._sessions[uid] = cur

    async def get_live_session(self, uid: int) -> dict | None:
        cur = self._sessions.get(uid)
        return dict(cur) if cur else None

    async def clear_live_session(self, uid: int) -> None:
        self._sessions.pop(uid, None)

    async def set_meta(self, k: str, v: str) -> None:
        self._meta[k] = v

    async def get_meta(self, k: str, default: str | None = None) -> str | None:
        return self._meta.get(k, default)

    async def close(self) -> None:
        return None

    @staticmethod
    def private_target(target_id: int, openid: str, name: str = "我的 QQ") -> Target:
        return Target(target_id=target_id, channel_type=ChannelType.OFFICIAL_QQ,
                      kind=TargetKind.PRIVATE, external_id=openid, display_name=name)
