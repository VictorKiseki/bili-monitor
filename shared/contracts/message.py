"""message.py —— 发什么（契约 v0，见 docs/05 第 3 节）"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    text: str
    image_url: str | None = None
    link_url: str | None = None
    # 2026-10-28 新增：卡片图本地路径（官方通道 msg_type=7 上传后发送）
    image_path: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
