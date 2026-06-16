"""速率限制器模块

实现令牌桶算法，支持精确的QPS控制。
同时提供忙等(busy-wait)模式以减少调度延迟。
"""
from __future__ import annotations

import math
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


class RateLimiter(ABC):
    """速率限制器抽象基类"""

    @abstractmethod
    def acquire(self, tokens: int = 1) -> float:
        """获取指定数量的令牌，阻塞直到令牌可用

        Args:
            tokens: 需要获取的令牌数

        Returns:
            实际等待的秒数
        """
        ...

    @abstractmethod
    def try_acquire(self, tokens: int = 1) -> bool:
        """非阻塞尝试获取令牌

        Args:
            tokens: 需要获取的令牌数

        Returns:
            是否获取成功
        """
        ...

    @abstractmethod
    def set_rate(self, rate_per_second: float) -> None:
        """动态调整速率

        Args:
            rate_per_second: 新的每秒速率
        """
        ...

    @abstractmethod
    def get_rate(self) -> float:
        """获取当前速率"""
        ...


@dataclass
class TokenBucketRateLimiter(RateLimiter):
    """令牌桶速率限制器

    核心原理：
    1. 桶以固定速率补充令牌 (rate_per_second 令牌/秒)
    2. 桶有最大容量，多余的令牌溢出丢弃
    3. 每个请求消耗N个令牌，令牌不足则阻塞

    精度控制：
    - 使用 perf_counter() 获取高精度时间（纳秒级）
    - 支持忙等模式 (busy_wait) 替代 sleep()，减少操作系统调度抖动
    - 忙等阈值 (busy_wait_threshold)：小于该时间使用忙等

    典型使用场景：
    - 恒定速率压测：固定 rate
    - 阶梯加压：定期调用 set_rate() 调整
    - 渐增模式：连续平滑调整 rate
    """

    rate_per_second: float
    max_burst: Optional[float] = None
    busy_wait: bool = True
    busy_wait_threshold: float = 0.005

    def __post_init__(self) -> None:
        if self.rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")

        # 桶容量：默认 = 1秒的令牌量（允许瞬间1秒的突发）
        if self.max_burst is None:
            self.max_burst = float(self.rate_per_second)

        # 初始令牌设为 0，从 0 开始补充，避免初始突发导致前几秒 QPS 翻倍
        # 这样稳态 QPS 更准确地等于 rate_per_second
        self._tokens: float = 0.0
        self._last_refill_time: float = time.perf_counter()
        self._lock = threading.Lock()
        self._stopped = False

    def _refill(self) -> None:
        """根据逝去时间补充令牌"""
        now = time.perf_counter()
        elapsed = now - self._last_refill_time
        new_tokens = elapsed * self.rate_per_second

        if new_tokens > 0:
            self._tokens = min(self.max_burst or 0.0, self._tokens + new_tokens)
            self._last_refill_time = now

    def acquire(self, tokens: int = 1) -> float:
        """获取令牌，支持忙等减少调度延迟"""
        if self._stopped:
            return 0.0

        if tokens <= 0:
            return 0.0

        wait_start = time.perf_counter()

        while True:
            with self._lock:
                self._refill()

                if self._tokens >= tokens:
                    self._tokens -= tokens
                    break

                # 计算需要等待的时间（纳秒级精度）
                deficit = tokens - self._tokens
                wait_time = deficit / self.rate_per_second

            # 等待策略：短等待用忙等，长等待用sleep
            if self.busy_wait and wait_time <= self.busy_wait_threshold:
                # 忙等：精度高，但占用CPU
                end_time = time.perf_counter() + wait_time
                while time.perf_counter() < end_time:
                    pass
            else:
                # 精确睡眠：稍微提前醒，然后用忙等收尾
                if wait_time > self.busy_wait_threshold:
                    sleep_time = wait_time - self.busy_wait_threshold
                    time.sleep(max(0, sleep_time))
                remaining = (wait_start + wait_time) - time.perf_counter()
                if remaining > 0:
                    end_time = time.perf_counter() + remaining
                    while time.perf_counter() < end_time:
                        pass

        return time.perf_counter() - wait_start

    def try_acquire(self, tokens: int = 1) -> bool:
        """非阻塞获取令牌"""
        if self._stopped or tokens <= 0:
            return False

        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def set_rate(self, rate_per_second: float) -> None:
        """动态调整速率（支持阶梯/渐增模式）"""
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")

        with self._lock:
            self._refill()  # 先按旧速率补充

            # 按比例调整当前令牌数，避免突变
            if self.rate_per_second > 0:
                ratio = rate_per_second / self.rate_per_second
                self._tokens = min(rate_per_second, self._tokens * ratio)

            self.rate_per_second = rate_per_second
            self.max_burst = float(rate_per_second)

    def get_rate(self) -> float:
        return self.rate_per_second

    def stop(self) -> None:
        """停止速率限制器，所有后续 acquire 立即返回"""
        self._stopped = True

    def reset(self) -> None:
        """重置状态"""
        with self._lock:
            self._tokens = float(self.max_burst or self.rate_per_second)
            self._last_refill_time = time.perf_counter()
            self._stopped = False


class LeakyBucketRateLimiter(RateLimiter):
    """漏桶速率限制器（固定出流速率，无突发）

    与令牌桶的区别：
    - 令牌桶允许突发（只要桶里有足够令牌）
    - 漏桶严格均匀出流，请求之间间隔精确相等
    适合需要绝对均匀流量的场景。
    """

    def __init__(self, rate_per_second: float):
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self._rate = rate_per_second
        self._interval = 1.0 / rate_per_second
        self._last_release = time.perf_counter() - self._interval
        self._lock = threading.Lock()

    def acquire(self, tokens: int = 1) -> float:
        wait_total = 0.0
        for _ in range(tokens):
            wait_start = time.perf_counter()
            with self._lock:
                next_release = self._last_release + self._interval
                now = time.perf_counter()
                if now < next_release:
                    wait = next_release - now
                    # 忙等精确控制
                    end_time = next_release
                    while time.perf_counter() < end_time:
                        pass
                self._last_release = time.perf_counter()
            wait_total += time.perf_counter() - wait_start
        return wait_total

    def try_acquire(self, tokens: int = 1) -> bool:
        with self._lock:
            now = time.perf_counter()
            next_release = self._last_release + self._interval * tokens
            if now >= next_release:
                self._last_release = now
                return True
            return False

    def set_rate(self, rate_per_second: float) -> None:
        with self._lock:
            self._rate = rate_per_second
            self._interval = 1.0 / rate_per_second

    def get_rate(self) -> float:
        return self._rate
