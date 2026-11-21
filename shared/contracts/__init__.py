"""shared.contracts —— 契约 v0（docs/05），T1–T5 唯一共享数据结构。"""
from .channel import Channel, ChannelCapability, SendResult, SendStatus
from .event import Event, EventType, make_event_id
from .message import RenderedMessage
from .store import Delivery, Store, Subject, SubjectState
from .target import ChannelType, Target, TargetKind

__all__ = [
    "Event", "EventType", "make_event_id",
    "Target", "TargetKind", "ChannelType",
    "RenderedMessage",
    "Channel", "ChannelCapability", "SendResult", "SendStatus",
    "Store", "Subject", "SubjectState", "Delivery",
]
