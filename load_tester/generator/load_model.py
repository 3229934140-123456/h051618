"""负载模型模块

定义多种负载生成模式，控制压力随时间的变化曲线：

1. ConstantLoadModel - 恒定负载：固定并发 + 固定QPS
2. StepLoadModel - 阶梯加压：分阶段逐步增加压力
3. RampUpLoadModel - 平滑渐增：线性增长到目标值
4. SpikeLoadModel - 尖峰模型：突发压力测试
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from .rate_limiter import RateLimiter, TokenBucketRateLimiter
from .worker import WorkerPool


class LoadModel(ABC):
    """负载模型抽象基类

    负载模型负责：
    1. 控制 WorkerPool 的并发数（虚拟用户数 VU）
    2. 控制 RateLimiter 的 QPS 速率
    3. 定义压测总时长
    4. 在每个阶段通过回调通知上层
    """

    def __init__(self, name: str):
        self.name = name
        self._stop_event = threading.Event()
        self._is_running = False
        self._controller_thread: Optional[threading.Thread] = None

    @abstractmethod
    def apply(
        self,
        worker_pool: WorkerPool,
        rate_limiter: Optional[RateLimiter] = None,
        on_phase_change: Optional[Callable[[str, dict], None]] = None,
        on_progress: Optional[Callable[[float, dict], None]] = None,
    ) -> None:
        """应用负载模型

        Args:
            worker_pool: 工作池，用于调整并发数
            rate_limiter: 速率限制器，用于调整QPS
            on_phase_change: 阶段变化回调 (phase_name, phase_info)
            on_progress: 进度回调 (progress_pct, current_state)
        """
        ...

    @abstractmethod
    def get_total_duration(self) -> float:
        """获取总压测时长（秒）"""
        ...

    def stop(self) -> None:
        """停止负载模型控制"""
        self._stop_event.set()
        if self._controller_thread and self._controller_thread.is_alive():
            self._controller_thread.join(timeout=2.0)

    @property
    def is_running(self) -> bool:
        return self._is_running


@dataclass
class ConstantLoadModel(LoadModel):
    """恒定负载模型

    维持固定的并发用户数和QPS，持续指定时长。

    适用场景：
    - 稳定性测试（长时间运行）
    - 确定系统在特定负载下的性能基线

    参数：
    - duration: 总持续时间（秒）
    - concurrency: 并发用户数（VU）
    - qps: 每秒请求数（None = 不限速）
    - warmup: 预热时间（秒），在此期间不统计结果
    """

    duration: float = 60.0
    concurrency: int = 10
    qps: Optional[float] = None
    warmup: float = 5.0

    def __post_init__(self) -> None:
        LoadModel.__init__(self, "constant")
        if self.duration <= 0:
            raise ValueError("duration must be positive")
        if self.concurrency <= 0:
            raise ValueError("concurrency must be positive")
        if self.qps is not None and self.qps <= 0:
            raise ValueError("qps must be positive")

    def get_total_duration(self) -> float:
        return self.duration

    def apply(
        self,
        worker_pool: WorkerPool,
        rate_limiter: Optional[RateLimiter] = None,
        on_phase_change: Optional[Callable[[str, dict], None]] = None,
        on_progress: Optional[Callable[[float, dict], None]] = None,
    ) -> None:
        self._stop_event.clear()
        self._is_running = True

        def _controller():
            try:
                start_time = time.time()
                phases: List[Tuple[str, float]] = []

                if self.warmup > 0:
                    phases.append(("warmup", self.warmup))
                phases.append(("steady", self.duration - self.warmup))

                elapsed_total = 0.0

                for phase_name, phase_duration in phases:
                    if self._stop_event.is_set():
                        break

                    # 调整到目标并发
                    worker_pool.scale_to(self.concurrency)

                    # 如果有速率限制，应用QPS
                    if rate_limiter and self.qps:
                        rate_limiter.set_rate(self.qps)

                    phase_info = {
                        "phase": phase_name,
                        "duration": phase_duration,
                        "concurrency": self.concurrency,
                        "qps": self.qps,
                    }
                    if on_phase_change:
                        on_phase_change(phase_name, phase_info)

                    # 逐小步报告进度，支持及时响应停止信号
                    phase_start = time.time()
                    step = min(0.1, phase_duration / 100) if phase_duration > 0 else 0
                    while not self._stop_event.is_set():
                        phase_elapsed = time.time() - phase_start
                        if phase_elapsed >= phase_duration:
                            break

                        elapsed_total = time.time() - start_time
                        progress_pct = min(100.0, (elapsed_total / self.duration) * 100)
                        if on_progress:
                            on_progress(
                                progress_pct,
                                {
                                    "phase": phase_name,
                                    "phase_elapsed": phase_elapsed,
                                    "phase_remaining": phase_duration - phase_elapsed,
                                    "active_workers": worker_pool.active_workers,
                                    "total_iterations": worker_pool.total_iterations,
                                },
                            )
                        time.sleep(step)

            finally:
                self._is_running = False

        self._controller_thread = threading.Thread(
            target=_controller, name="ConstantLoad-Controller", daemon=True,
        )
        self._controller_thread.start()


@dataclass
class StepLoadModel(LoadModel):
    """阶梯加压负载模型

    分阶段逐步增加压力，每个阶段持续指定时间。
    用于发现系统的性能拐点（随着压力增加，QPS/延迟的突变点）。

    参数：
    - steps: 阶梯配置列表，每个元素 (duration, concurrency, qps)
    - start_concurrency: 初始并发数
    - start_qps: 初始QPS

    典型 steps 配置示例（5个阶梯，每个120秒）：
    steps = [
        (120, 10, 100),
        (120, 25, 250),
        (120, 50, 500),
        (120, 100, 1000),
        (120, 200, 2000),
    ]
    """

    steps: List[Tuple[float, int, Optional[float]]] = field(default_factory=list)
    warmup: float = 5.0

    def __post_init__(self) -> None:
        LoadModel.__init__(self, "step")
        if not self.steps:
            raise ValueError("steps must not be empty")
        for i, (d, c, q) in enumerate(self.steps):
            if d <= 0:
                raise ValueError(f"Step {i} duration must be positive")
            if c <= 0:
                raise ValueError(f"Step {i} concurrency must be positive")
            if q is not None and q <= 0:
                raise ValueError(f"Step {i} qps must be positive")

    def get_total_duration(self) -> float:
        return self.warmup + sum(d for d, _, _ in self.steps)

    def apply(
        self,
        worker_pool: WorkerPool,
        rate_limiter: Optional[RateLimiter] = None,
        on_phase_change: Optional[Callable[[str, dict], None]] = None,
        on_progress: Optional[Callable[[float, dict], None]] = None,
    ) -> None:
        self._stop_event.clear()
        self._is_running = True
        total_duration = self.get_total_duration()

        def _controller():
            try:
                start_time = time.time()
                step_count = len(self.steps)

                # 预热阶段
                if self.warmup > 0 and not self._stop_event.is_set():
                    first_duration, first_concurrency, first_qps = self.steps[0]
                    worker_pool.scale_to(first_concurrency)
                    if rate_limiter and first_qps:
                        rate_limiter.set_rate(first_qps)

                    warmup_info = {
                        "phase": "warmup",
                        "step_index": 0,
                        "duration": self.warmup,
                        "concurrency": first_concurrency,
                        "qps": first_qps,
                    }
                    if on_phase_change:
                        on_phase_change("warmup", warmup_info)

                    self._wait_phase(
                        self.warmup,
                        total_duration,
                        start_time,
                        worker_pool,
                        "warmup",
                        0,
                        on_progress,
                    )

                # 执行各个阶梯
                for step_idx, (step_duration, concurrency, qps) in enumerate(self.steps):
                    if self._stop_event.is_set():
                        break

                    # 调整压力水平
                    worker_pool.scale_to(concurrency)
                    if rate_limiter and qps:
                        rate_limiter.set_rate(qps)

                    phase_info = {
                        "phase": f"step_{step_idx + 1}",
                        "step_index": step_idx,
                        "step_count": step_count,
                        "duration": step_duration,
                        "concurrency": concurrency,
                        "qps": qps,
                    }
                    if on_phase_change:
                        on_phase_change(f"step_{step_idx + 1}", phase_info)

                    self._wait_phase(
                        step_duration,
                        total_duration,
                        start_time,
                        worker_pool,
                        f"step_{step_idx + 1}",
                        step_idx,
                        on_progress,
                    )

            finally:
                self._is_running = False

        self._controller_thread = threading.Thread(
            target=_controller, name="StepLoad-Controller", daemon=True,
        )
        self._controller_thread.start()

    def _wait_phase(
        self,
        phase_duration: float,
        total_duration: float,
        start_time: float,
        worker_pool: WorkerPool,
        phase_name: str,
        step_index: int,
        on_progress: Optional[Callable],
    ) -> None:
        """等待一个阶段完成，期间报告进度"""
        phase_start = time.time()
        step = min(0.1, phase_duration / 100) if phase_duration > 0 else 0

        while not self._stop_event.is_set():
            phase_elapsed = time.time() - phase_start
            if phase_elapsed >= phase_duration:
                break

            elapsed_total = time.time() - start_time
            progress_pct = min(100.0, (elapsed_total / total_duration) * 100)
            if on_progress:
                on_progress(
                    progress_pct,
                    {
                        "phase": phase_name,
                        "step_index": step_index,
                        "phase_elapsed": phase_elapsed,
                        "phase_remaining": phase_duration - phase_elapsed,
                        "active_workers": worker_pool.active_workers,
                        "total_iterations": worker_pool.total_iterations,
                    },
                )
            time.sleep(step)


@dataclass
class RampUpLoadModel(LoadModel):
    """平滑渐增负载模型

    从初始值线性增长到目标值，相比阶梯模型更平滑，
    适合观察系统在压力渐增下的连续表现。

    参数：
    - duration: 总时长
    - start_concurrency: 起始并发数
    - end_concurrency: 结束并发数
    - start_qps: 起始QPS
    - end_qps: 结束QPS
    - adjust_interval: 调整间隔（秒），越小越平滑
    """

    duration: float = 300.0
    start_concurrency: int = 1
    end_concurrency: int = 100
    start_qps: Optional[float] = None
    end_qps: Optional[float] = None
    adjust_interval: float = 1.0
    warmup: float = 5.0
    hold_end_duration: float = 30.0

    def __post_init__(self) -> None:
        LoadModel.__init__(self, "ramp")
        if self.duration <= 0:
            raise ValueError("duration must be positive")
        if self.start_concurrency <= 0 or self.end_concurrency <= 0:
            raise ValueError("concurrency must be positive")
        if self.adjust_interval <= 0:
            raise ValueError("adjust_interval must be positive")

    def get_total_duration(self) -> float:
        return self.warmup + self.duration + self.hold_end_duration

    def apply(
        self,
        worker_pool: WorkerPool,
        rate_limiter: Optional[RateLimiter] = None,
        on_phase_change: Optional[Callable[[str, dict], None]] = None,
        on_progress: Optional[Callable[[float, dict], None]] = None,
    ) -> None:
        self._stop_event.clear()
        self._is_running = True
        total_duration = self.get_total_duration()

        def _controller():
            try:
                start_time = time.time()

                # 预热
                if self.warmup > 0 and not self._stop_event.is_set():
                    worker_pool.scale_to(self.start_concurrency)
                    if rate_limiter and self.start_qps:
                        rate_limiter.set_rate(self.start_qps)

                    if on_phase_change:
                        on_phase_change("warmup", {"phase": "warmup", "duration": self.warmup})
                    self._sleep_with_progress(
                        self.warmup, total_duration, start_time,
                        worker_pool, "warmup", on_progress,
                    )

                # Ramp-up 阶段：线性增长
                if not self._stop_event.is_set():
                    ramp_start = time.time()
                    if on_phase_change:
                        on_phase_change(
                            "ramp",
                            {
                                "phase": "ramp",
                                "duration": self.duration,
                                "start_concurrency": self.start_concurrency,
                                "end_concurrency": self.end_concurrency,
                                "start_qps": self.start_qps,
                                "end_qps": self.end_qps,
                            },
                        )

                    while not self._stop_event.is_set():
                        ramp_elapsed = time.time() - ramp_start
                        if ramp_elapsed >= self.duration:
                            break

                        progress = min(1.0, ramp_elapsed / self.duration)

                        # 线性插值计算当前目标值
                        target_concurrency = int(
                            self.start_concurrency
                            + (self.end_concurrency - self.start_concurrency) * progress
                        )
                        worker_pool.scale_to(max(1, target_concurrency))

                        if rate_limiter and self.start_qps and self.end_qps:
                            target_qps = (
                                self.start_qps
                                + (self.end_qps - self.start_qps) * progress
                            )
                            rate_limiter.set_rate(max(0.1, target_qps))

                        elapsed_total = time.time() - start_time
                        progress_pct = min(100.0, (elapsed_total / total_duration) * 100)
                        if on_progress:
                            on_progress(
                                progress_pct,
                                {
                                    "phase": "ramp",
                                    "ramp_progress": progress,
                                    "current_concurrency": target_concurrency,
                                    "current_qps": (
                                        self.start_qps + (self.end_qps - self.start_qps) * progress
                                        if self.start_qps and self.end_qps else None
                                    ),
                                    "active_workers": worker_pool.active_workers,
                                    "total_iterations": worker_pool.total_iterations,
                                },
                            )

                        time.sleep(self.adjust_interval)

                # 保持在峰值
                if self.hold_end_duration > 0 and not self._stop_event.is_set():
                    worker_pool.scale_to(self.end_concurrency)
                    if rate_limiter and self.end_qps:
                        rate_limiter.set_rate(self.end_qps)

                    if on_phase_change:
                        on_phase_change(
                            "hold",
                            {"phase": "hold", "duration": self.hold_end_duration},
                        )
                    self._sleep_with_progress(
                        self.hold_end_duration, total_duration, start_time,
                        worker_pool, "hold", on_progress,
                    )

            finally:
                self._is_running = False

        self._controller_thread = threading.Thread(
            target=_controller, name="RampLoad-Controller", daemon=True,
        )
        self._controller_thread.start()

    def _sleep_with_progress(
        self,
        duration: float,
        total_duration: float,
        start_time: float,
        worker_pool: WorkerPool,
        phase_name: str,
        on_progress: Optional[Callable],
    ) -> None:
        phase_start = time.time()
        step = min(0.1, duration / 100) if duration > 0 else 0
        while not self._stop_event.is_set():
            phase_elapsed = time.time() - phase_start
            if phase_elapsed >= duration:
                break
            elapsed_total = time.time() - start_time
            progress_pct = min(100.0, (elapsed_total / total_duration) * 100)
            if on_progress:
                on_progress(
                    progress_pct,
                    {
                        "phase": phase_name,
                        "active_workers": worker_pool.active_workers,
                        "total_iterations": worker_pool.total_iterations,
                    },
                )
            time.sleep(step)


@dataclass
class SpikeLoadModel(LoadModel):
    """尖峰负载模型

    用于测试系统在突发流量下的表现（如秒杀场景）。
    流量模式：基线 -> 突发尖峰 -> 回落。
    """
    base_duration: float = 60.0
    spike_duration: float = 30.0
    base_concurrency: int = 10
    spike_concurrency: int = 200
    base_qps: Optional[float] = 50.0
    spike_qps: Optional[float] = 1000.0
    ramp_seconds: float = 5.0
    hold_count: int = 1
    warmup: float = 5.0

    def __post_init__(self) -> None:
        LoadModel.__init__(self, "spike")

    def get_total_duration(self) -> float:
        total = self.warmup
        for _ in range(self.hold_count):
            total += self.base_duration + self.ramp_seconds * 2 + self.spike_duration
        return total

    def apply(
        self,
        worker_pool: WorkerPool,
        rate_limiter: Optional[RateLimiter] = None,
        on_phase_change: Optional[Callable[[str, dict], None]] = None,
        on_progress: Optional[Callable[[float, dict], None]] = None,
    ) -> None:
        self._stop_event.clear()
        self._is_running = True
        total_duration = self.get_total_duration()

        def _controller():
            try:
                start_time = time.time()

                if self.warmup > 0:
                    worker_pool.scale_to(self.base_concurrency)
                    if rate_limiter and self.base_qps:
                        rate_limiter.set_rate(self.base_qps)
                    if on_phase_change:
                        on_phase_change("warmup", {"phase": "warmup"})
                    self._sleep_wp(self.warmup, total_duration, start_time, worker_pool, "warmup", on_progress)

                for i in range(self.hold_count):
                    if self._stop_event.is_set():
                        break

                    # 基线
                    worker_pool.scale_to(self.base_concurrency)
                    if rate_limiter and self.base_qps:
                        rate_limiter.set_rate(self.base_qps)
                    if on_phase_change:
                        on_phase_change(f"baseline_{i}", {"phase": f"baseline_{i}"})
                    self._sleep_wp(self.base_duration, total_duration, start_time, worker_pool, f"baseline_{i}", on_progress)

                    # Ramp-up to spike
                    if on_phase_change:
                        on_phase_change(f"ramp_up_{i}", {"phase": f"ramp_up_{i}"})
                    self._ramp(self.base_concurrency, self.spike_concurrency,
                               self.base_qps, self.spike_qps,
                               self.ramp_seconds,
                               worker_pool, rate_limiter,
                               total_duration, start_time,
                               f"ramp_up_{i}", on_progress)

                    # 尖峰
                    if on_phase_change:
                        on_phase_change(f"spike_{i}", {"phase": f"spike_{i}"})
                    self._sleep_wp(self.spike_duration, total_duration, start_time, worker_pool, f"spike_{i}", on_progress)

                    # Ramp-down
                    if on_phase_change:
                        on_phase_change(f"ramp_down_{i}", {"phase": f"ramp_down_{i}"})
                    self._ramp(self.spike_concurrency, self.base_concurrency,
                               self.spike_qps, self.base_qps,
                               self.ramp_seconds,
                               worker_pool, rate_limiter,
                               total_duration, start_time,
                               f"ramp_down_{i}", on_progress)

            finally:
                self._is_running = False

        self._controller_thread = threading.Thread(
            target=_controller, name="SpikeLoad-Controller", daemon=True,
        )
        self._controller_thread.start()

    def _ramp(self, c_start, c_end, q_start, q_end, duration, pool, rl, total_dur, start_t, phase, cb):
        phase_start = time.time()
        step = min(0.2, duration / 20) if duration > 0 else 0.1
        while not self._stop_event.is_set():
            elapsed = time.time() - phase_start
            if elapsed >= duration:
                break
            p = min(1.0, elapsed / duration)
            c = int(c_start + (c_end - c_start) * p)
            pool.scale_to(c)
            if rl and q_start is not None and q_end is not None:
                rl.set_rate(q_start + (q_end - q_start) * p)
            if cb:
                total_elapsed = time.time() - start_t
                cb(min(100.0, (total_elapsed / total_dur) * 100), {"phase": phase})
            time.sleep(step)

    def _sleep_wp(self, duration, total_dur, start_t, pool, phase, cb):
        phase_start = time.time()
        step = min(0.2, duration / 50) if duration > 0 else 0.1
        while not self._stop_event.is_set():
            elapsed = time.time() - phase_start
            if elapsed >= duration:
                break
            if cb:
                total_elapsed = time.time() - start_t
                cb(min(100.0, (total_elapsed / total_dur) * 100),
                   {"phase": phase, "active_workers": pool.active_workers})
            time.sleep(step)
