"""store.py —— 存储抽象（契约 v0，见 docs/05 第 5 节）

相对草案新增了 read 侧的两个方法 get_event / get_target：
调度器拿到 Delivery 后必须能取到 Event 与 Target 才能渲染和发送。
（草案只增不删，属于允许的加法，已在 docs/05 第 10 节记录。）

2026-11-07 增补（都只是加法，旧实现不实现也不影响启动）：
· get_events / get_targets —— 批量读，消掉 dispatcher 的 N+1；
· seen_ids(uid, since_ts) —— 去重水位支持时间窗；
· reclaim_stale_claims / prune —— 两阶段领取的僵尸回收与 TTL 清理。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .channel import SendStatus
from .event import Event
from .target import Target


@dataclass(frozen=True, slots=True)
class Subject:
    uid: int
    name: str
    room_id: int | None
    enabled: bool = True
    push_live: bool = True        # 2026-11-07：该主播是否推送直播（开播/下播）
    push_dynamic: bool = True     # 2026-11-07：是否推送动态（含视频/专栏/转发）
    weekly_on: bool = True        # 2026-11-07：是否对该主播做日程表（周表）监控


@dataclass(frozen=True, slots=True)
class SubjectState:
    uid: int
    room_id: int | None
    live_status: int
    last_live_start_ts: int | None = None
    last_dynamic_id: str | None = None
    last_poll_ts: int | None = None


@dataclass(frozen=True, slots=True)
class Delivery:
    delivery_id: int
    event_id: str
    target_id: int
    attempts: int
    next_at: float = 0.0
    # pending / sending / sent / failed / dismissed（sending = 已领取未落终态；
    # dismissed = 用户确认「不发了」的终态，不计失败、不重发，随事件过期清掉）
    status: str = "pending"
    last_error: str | None = None
    # H4（2026-11-14）：本次领取的时刻（delivery.claimed_at）。finish() 拿它当 fencing 令牌：
    # 只有「仍由这一次领取持有」的记录才能落结果，避免迟到结果覆盖别人的新领取。
    claim_at: float | None = None


class Store(Protocol):
    async def list_subjects(self, enabled_only: bool = True) -> list[Subject]: ...
    async def get_subject(self, uid: int) -> Subject | None: ...
    # 2026-11-10：名单对账用。upsert 的冲突分支不碰 enabled，停用已有行只能走这里。
    async def set_subject_enabled(self, uid: int, enabled: bool) -> bool: ...
    async def save_subject_state(self, state: SubjectState) -> None: ...

    async def insert_event(self, ev: Event) -> bool: ...
    async def get_event(self, event_id: str) -> Event | None: ...
    # 2026-11-07（M4）：批量读，一次 WHERE IN / JOIN 取回全部，避免逐条 N+1
    async def get_events(self, event_ids: list[str]) -> dict[str, Event]: ...

    async def enqueue(self, event_id: str, target_ids: list[int]) -> int: ...
    async def claim_due(self, limit: int) -> list[Delivery]: ...
    # 2026-11-07（H3）：重试策略只在 Dispatcher；Store 只执行算好的结果。
    # attempts / next_at 由调用方给；都不给时实现按自身旧策略兜底（兼容老脚本）。
    # 2026-11-07（M11）：raw 是渲染失败的输入原文；缺省 None，旧调用不受影响，
    # 实现只在非 None 时落库，且不覆盖已有原文。
    # H4（2026-11-14）：claim_at 是本次领取的 claimed_at；给了就按它做 fencing，
    # 不给（老脚本/补写原文）按 delivery_id 直接更新。
    async def finish(self, delivery_id: int, status: SendStatus, error: str | None,
                     *, attempts: int | None = None, next_at: float | None = None,
                     raw: str | None = None, claim_at: float | None = None) -> bool: ...
    # 2026-11-07（H2）：把 status='sending' 且 claimed_at 超时的投递改回 pending，返回条数
    async def reclaim_stale_claims(self, older_than_s: int = 300) -> int: ...
    # 2026-11-07（M12）：TTL 清理，返回各表删除条数；绝不删 pending/sending 的投递
    async def prune(self, *, seen_days: int = 90, event_days: int = 180) -> dict[str, int]: ...
    # 2026-11-07：这条事件还有几条投递没到终态（0 = 可以删卡片图缓存了）
    async def open_deliveries(self, event_id: str) -> int: ...

    async def get_target(self, target_id: int) -> Target | None: ...
    # 2026-11-07（M4）：批量读投递目标
    async def get_targets(self, target_ids: list[int]) -> dict[int, Target]: ...

    # 动态去重水位（2026-11-07，H1）：since_ts 非空时只看 pub_ts>=since_ts；None = 全量（旧行为）
    async def seen_ids(self, uid: int, since_ts: int | None = None) -> set[str]: ...
    async def mark_seen(self, uid: int, items: list[tuple[str, int]]) -> None: ...

    # 心跳（守护进程判断"进程是否还活着"用），2026-10-28 增量
    async def set_meta(self, k: str, v: str) -> None: ...
    async def get_meta(self, k: str, default: str | None = None) -> str | None: ...

    async def close(self) -> None: ...
