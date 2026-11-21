"""target.py —— 推送给谁（契约 v0，见 docs/05 第 2 节）"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ChannelType(str, Enum):
    OFFICIAL_QQ = "official_qq"
    ONEBOT = "onebot"


class TargetKind(str, Enum):
    GROUP = "group"
    PRIVATE = "private"


@dataclass(frozen=True, slots=True)
class Target:
    target_id: int
    channel_type: ChannelType
    kind: TargetKind
    external_id: str          # 官方：group_openid / user_openid
    display_name: str
    enabled: bool = True
