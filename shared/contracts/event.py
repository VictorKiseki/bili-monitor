"""event.py —— 唯一跨模块数据（契约 v0，见 docs/05 第 1 节）

冻结规则：字段只增不删。改动需 2 人同意，PR 标题前缀 [contract]。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    LIVE_START = "live_start"   # 开播
    LIVE_END = "live_end"       # 下播
    DYNAMIC = "dynamic"         # 动态（含转发）
    VIDEO = "video"             # 视频投稿
    ARTICLE = "article"         # 专栏
    # 2026-10-27 决策：LIVE_RESERVE 已移除，直播预约永久不做


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str                      # {uid}:{type}:{dedup_key}
    subject_uid: int
    type: EventType
    occurred_at: int                   # B 站 pub_ts / live_time，10 位秒
    dedup_key: str
    title: str
    url: str
    cover_url: str | None = None
    summary: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def uname(self) -> str:
        """模板里的 {uname}。由 T1 采集在 extra["uname"] 里带上昵称。"""
        return str(self.extra.get("uname") or f"UID{self.subject_uid}")

    @property
    def subtype(self) -> str:
        return str(self.extra.get("subtype") or "")


def make_event_id(uid: int, t: EventType, dedup_key: str) -> str:
    return f"{uid}:{t.value}:{dedup_key}"
