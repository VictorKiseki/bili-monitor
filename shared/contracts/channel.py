"""channel.py —— 通道抽象（契约 v0，见 docs/05 第 4 节）"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from .message import RenderedMessage
from .target import ChannelType, Target


class SendStatus(str, Enum):
    OK = "ok"        # 成功
    RETRY = "retry"  # 可重试（限流、网络抖动）
    DROP = "drop"    # 不可重试（openid 失效、权限不足）


@dataclass(frozen=True, slots=True)
class SendResult:
    status: SendStatus
    error_code: str | None = None
    error_msg: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChannelCapability:
    max_text_len: int
    supports_image: bool
    supports_markdown: bool
    min_interval_s: float


class Channel(Protocol):
    name: ChannelType

    def capability(self) -> ChannelCapability: ...

    async def send(self, target: Target, msg: RenderedMessage) -> SendResult: ...

    async def health(self) -> bool: ...
