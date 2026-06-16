"""指标采集器

负责从工作线程收集样本数据，通过无锁队列异步传递给聚合器。
支持多种输出目标（Sink）：统计聚合、日志、实时监控等。

设计要点：
1. 无锁队列传递：使用标准库 queue.Queue（已线程安全，内部有锁但实现高效）
2. 批量处理：后台线程批量从队列取出，减少锁竞争
3. 多Sink分发：一个样本可同时发送给多个Sink（统计+日志+监控）
4. 背压控制：队列满时丢弃最旧样本，避免内存溢出
5. 零拷贝：样本对象直接传递引用，不做序列化
"""
from __future__ import annotations

import queue
import threading
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, DefaultDict, Dict, List, Optional

from .sample import Sample, SampleStatus, SampleType


class MetricsSink(ABC):
    """指标输出目标抽象基类"""

    @abstractmethod
    def write(self, samples: List[Sample]) -> None:
        """写入一批样本数据"""
        ...

    def flush(self) -> None:
        """刷新缓冲（可选实现）"""
        pass

    def close(self) -> None:
        """关闭Sink（可选实现）"""
        pass


class RealTimeSink(MetricsSink):
    """实时回调Sink

    每批样本通过回调函数通知上层。
    可用于实现实时监控仪表盘。
    """

    def __init__(self, callback: Callable[[List[Sample]], None]):
        self._callback = callback

    def write(self, samples: List[Sample]) -> None:
        try:
            self._callback(samples)
        except Exception:
            pass


class RecordingSink(MetricsSink):
    """内存记录Sink

    将所有样本记录在内存列表中。
    仅用于小规模测试，大规模测试会占用过多内存。
    """

    def __init__(self, max_samples: int = 1_000_000):
        self._samples: List[Sample] = []
        self._lock = threading.Lock()
        self._max_samples = max_samples

    def write(self, samples: List[Sample]) -> None:
        with self._lock:
            self._samples.extend(samples)
            # 超过上限时丢弃最早的
            if len(self._samples) > self._max_samples:
                overflow = len(self._samples) - self._max_samples
                del self._samples[:overflow]

    def get_samples(self) -> List[Sample]:
        with self._lock:
            return list(self._samples)

    def clear(self) -> None:
        with self._lock:
            self._samples.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._samples)


class WindowedSink(MetricsSink):
    """滑动窗口Sink

    仅保留最近N秒的样本。
    用于计算实时指标（如最近1分钟的QPS、延迟分位等）。
    """

    def __init__(self, window_seconds: float = 60.0):
        self._window = window_seconds
        self._samples: List[Sample] = []
        self._lock = threading.Lock()

    def write(self, samples: List[Sample]) -> None:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            # 添加新样本
            self._samples.extend(samples)
            # 移除窗口外的旧样本
            while self._samples and self._samples[0].timestamp < cutoff:
                self._samples.pop(0)

    def get_window_samples(self) -> List[Sample]:
        with self._lock:
            return list(self._samples)

    def get_metrics(self) -> Dict[str, Any]:
        """快速计算窗口内的核心指标"""
        with self._lock:
            samples = list(self._samples)

        if not samples:
            return {"count": 0, "qps": 0.0, "avg_latency_ms": 0.0, "error_rate": 0.0}

        count = len(samples)
        window_duration = max(0.001, self._window)
        qps = count / window_duration
        errors = sum(1 for s in samples if s.is_error)
        error_rate = errors / count
        avg_latency_ms = sum(s.latency_ms for s in samples) / count

        return {
            "count": count,
            "qps": qps,
            "avg_latency_ms": avg_latency_ms,
            "error_rate": error_rate,
            "errors": errors,
            "successes": count - errors,
        }


@dataclass
class CollectorStats:
    """采集器自身的统计信息，用于监控压测工具性能"""
    total_samples: int = 0
    dropped_samples: int = 0
    queue_size: int = 0
    batches_processed: int = 0
    avg_batch_size: float = 0.0
    processing_errors: int = 0


class MetricsCollector:
    """指标采集器

    作为压测系统的"数据总线"：
    - 接收 Worker 产生的 Sample
    - 通过内存队列异步缓冲
    - 批量分发到各个 Sink
    - 监控自身状态，避免成为瓶颈

    典型用法：
    ```
    collector = MetricsCollector(max_queue_size=100000)
    collector.add_sink("stats", StatsAggregatorSink(aggregator))
    collector.add_sink("console", ConsoleSink())
    collector.start()

    # Worker 线程中
    collector.record(sample)  # 立即返回，不阻塞

    collector.stop()
    ```
    """

    def __init__(
        self,
        max_queue_size: int = 100000,
        batch_size: int = 1000,
        batch_timeout: float = 0.01,
        drop_on_full: bool = True,
    ):
        """
        Args:
            max_queue_size: 队列最大容量，超过后按策略处理
            batch_size: 每次批量处理的样本数
            batch_timeout: 批量等待超时（秒），即使不满也处理
            drop_on_full: 队列满时 True=丢弃最旧 False=阻塞写入
        """
        self._queue: "queue.Queue[Sample]" = queue.Queue(maxsize=max_queue_size)
        self._sinks: Dict[str, MetricsSink] = {}
        self._sink_order: List[str] = []
        self._batch_size = batch_size
        self._batch_timeout = batch_timeout
        self._drop_on_full = drop_on_full

        self._stats = CollectorStats()
        self._stats_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._is_running = False
        self._last_dropped_warning = 0.0

    def add_sink(self, name: str, sink: MetricsSink) -> None:
        """添加输出目标"""
        if name in self._sinks:
            raise ValueError(f"Sink '{name}' already exists")
        self._sinks[name] = sink
        self._sink_order.append(name)

    def remove_sink(self, name: str) -> Optional[MetricsSink]:
        """移除输出目标"""
        sink = self._sinks.pop(name, None)
        if sink and name in self._sink_order:
            self._sink_order.remove(name)
        return sink

    def has_sink(self, name: str) -> bool:
        return name in self._sinks

    def record(self, sample: Sample) -> None:
        """记录一个样本（线程安全，非阻塞）

        这是 Worker 线程调用的核心入口。
        必须足够快，避免影响压测精度。
        """
        try:
            if self._drop_on_full:
                try:
                    self._queue.put_nowait(sample)
                except queue.Full:
                    # 队列已满，丢弃最旧的一条再尝试
                    with self._stats_lock:
                        self._stats.dropped_samples += 1
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self._queue.put_nowait(sample)
                    except queue.Full:
                        # 仍然满，直接丢弃当前样本
                        with self._stats_lock:
                            self._stats.dropped_samples += 1

                    # 限制警告频率
                    now = time.time()
                    if now - self._last_dropped_warning > 5.0:
                        self._last_dropped_warning = now
            else:
                # 阻塞模式：可能影响压测精度，慎用
                self._queue.put(sample, timeout=1.0)

        except Exception as e:
            with self._stats_lock:
                self._stats.processing_errors += 1

    def record_batch(self, samples: List[Sample]) -> None:
        """批量记录样本"""
        for s in samples:
            self.record(s)

    def record_from_worker_result(self, worker_result) -> None:
        """从 WorkerResult 生成并记录样本

        方便 Worker 回调使用，一次生成多个相关样本。
        """
        scenario_result = worker_result.scenario_result
        wid = worker_result.worker_id

        # 1. 场景级样本
        self.record(Sample.from_scenario_result(scenario_result, wid))

        # 2. 步骤级样本
        for step_result in scenario_result.steps:
            self.record(Sample.from_step_result(step_result, wid))

    def start(self) -> None:
        """启动采集器后台处理线程"""
        if self._is_running:
            return

        self._stop_event.clear()
        self._is_running = True
        self._worker_thread = threading.Thread(
            target=self._processing_loop,
            name="MetricsCollector-Worker",
            daemon=True,
        )
        self._worker_thread.start()

    def stop(self, flush: bool = True, timeout: float = 5.0) -> None:
        """停止采集器

        Args:
            flush: 是否处理队列中剩余的样本
            timeout: 最长等待时间
        """
        self._stop_event.set()

        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)

        # flush 剩余样本
        if flush:
            self._flush_remaining()

        # 关闭所有 sinks
        for name in self._sink_order:
            try:
                self._sinks[name].flush()
                self._sinks[name].close()
            except Exception:
                pass

        self._is_running = False

    def get_stats(self) -> CollectorStats:
        """获取采集器自身统计"""
        with self._stats_lock:
            stats = CollectorStats(
                total_samples=self._stats.total_samples,
                dropped_samples=self._stats.dropped_samples,
                queue_size=self._queue.qsize(),
                batches_processed=self._stats.batches_processed,
                avg_batch_size=self._stats.avg_batch_size,
                processing_errors=self._stats.processing_errors,
            )
        return stats

    def _processing_loop(self) -> None:
        """后台处理线程主循环"""
        batch: List[Sample] = []
        batch_deadline = 0.0

        while not self._stop_event.is_set():
            # 计算等待时间：如果已经有部分数据，等短一点
            if batch:
                wait_time = max(0.0, batch_deadline - time.time())
            else:
                wait_time = self._batch_timeout * 10

            try:
                sample = self._queue.get(timeout=wait_time)
                batch.append(sample)

                # 设置批次截止时间
                if len(batch) == 1:
                    batch_deadline = time.time() + self._batch_timeout

                # 批次满了，处理
                if len(batch) >= self._batch_size:
                    self._dispatch_batch(batch)
                    batch = []

            except queue.Empty:
                # 超时，处理已有数据
                if batch:
                    self._dispatch_batch(batch)
                    batch = []

        # 退出前处理 batch 中剩余的
        if batch:
            self._dispatch_batch(batch)

    def _dispatch_batch(self, batch: List[Sample]) -> None:
        """分发一批样本到所有 Sink"""
        if not batch:
            return

        # 更新统计
        with self._stats_lock:
            self._stats.total_samples += len(batch)
            self._stats.batches_processed += 1
            total_batches = self._stats.batches_processed
            current_avg = self._stats.avg_batch_size
            self._stats.avg_batch_size = (
                (current_avg * (total_batches - 1) + len(batch)) / total_batches
            )

        # 分发到每个 Sink
        for name in self._sink_order:
            sink = self._sinks.get(name)
            if sink:
                try:
                    sink.write(batch)
                except Exception:
                    with self._stats_lock:
                        self._stats.processing_errors += 1

    def _flush_remaining(self) -> None:
        """处理队列中所有剩余样本"""
        batch: List[Sample] = []
        while True:
            try:
                sample = self._queue.get_nowait()
                batch.append(sample)
                if len(batch) >= self._batch_size:
                    self._dispatch_batch(batch)
                    batch = []
            except queue.Empty:
                break
        if batch:
            self._dispatch_batch(batch)
