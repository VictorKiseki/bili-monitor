"""EchoChannel —— 只打日志的通道，无凭据也能联调（docs/05 第 4 节）"""
from __future__ import annotations

from shared.contracts import (
    ChannelCapability, ChannelType, RenderedMessage, SendResult, SendStatus, Target,
)


class EchoChannel:
    name = ChannelType.OFFICIAL_QQ

    def __init__(self, supports_image: bool = False):
        self._cap = ChannelCapability(
            max_text_len=900, supports_image=supports_image,
            supports_markdown=False, min_interval_s=0.3,
        )
        self.sent: list[tuple[str, str]] = []

    def capability(self) -> ChannelCapability:
        return self._cap

    async def send(self, target: Target, msg: RenderedMessage) -> SendResult:
        self.sent.append((target.display_name, msg.text))
        print(f"[echo→{target.display_name}] {msg.text}")
        return SendResult(status=SendStatus.OK)

    async def health(self) -> bool:
        return True
