"""B-1. 每目标令牌桶限流（docs/02 第 5.3 节：15 条/分钟，官方 20 条留余量）"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable


class TokenBucket:
    """容量 capacity，每 refill_per_s 秒补 1 个令牌。"""
    def __init__(
        self,
        capacity: int,
        refill_per_s: float,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.capacity = float(capacity)
        self.refill_per_s = float(refill_per_s)
        self._tokens = float(capacity)
        self._last = now()
        self._now = now
        self._sleep = sleep

    def _refill(self) -> None:
        t = self._now()
        elapsed = t - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_s)
            self._last = t

    def try_take(self) -> bool:
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    async def acquire(self) -> float:
        """拿到 1 个令牌，必要时等待。返回实际等待秒数。

        L4：若在 await self._sleep() 期间被 CancelledError 打断，已经扣掉的令牌无法回滚
        （tokens 是纯内存计数，取消点之后没有配对的「还令牌」逻辑）。这是可接受的：
          · 令牌桶是「限速的安全余量」而不是账本 —— 官方上限 20 条/分钟，这里配 15 条，
            丢一个令牌只会让后续发送稍慢一点，绝不会让实际速率超过官方限制；
          · 能打断这里的只有停机/取消，进程重新起来时桶本身就重建了，少一个令牌无意义；
          · 想做到可回滚就得在 except BaseException 里 self._tokens += 1，
            但那样取消路径也会写共享状态，反而更容易在停机瞬间写出难查的计数 bug。
        结论：丢令牌比丢一致性划算，这里保持现状并在此说明，不再加补偿逻辑。
        """
        waited = 0.0
        while not self.try_take():
            need = (1.0 - self._tokens) / self.refill_per_s
            need = max(need, 0.01)
            await self._sleep(need)     # 取消点：此处被取消则本轮不返还已扣令牌（见 docstring）
            waited += need
        return waited


class RateLimiter:
    """每个 target 一个桶；capacity 条/窗口秒。"""
    def __init__(
        self,
        capacity: int = 15,
        window_s: float = 60.0,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.capacity = capacity
        self.refill_per_s = capacity / window_s
        self._now = now
        self._sleep = sleep
        self._buckets: dict[int, TokenBucket] = {}

    def bucket(self, target_id: int) -> TokenBucket:
        b = self._buckets.get(target_id)
        if b is None:
            b = TokenBucket(self.capacity, self.refill_per_s, self._now, self._sleep)
            self._buckets[target_id] = b
        return b

    async def acquire(self, target_id: int) -> float:
        return await self.bucket(target_id).acquire()
