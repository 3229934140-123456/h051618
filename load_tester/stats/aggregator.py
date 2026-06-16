"""统计聚合模块

实时聚合指标采集器产生的样本数据：

1. PercentileMetrics - 延迟分布（通过 HDR 直方图）
2. ThroughputMetrics  - 吞吐量（每秒请求数）
3. ErrorMetrics       - 错误率（按错误类型、状态码分类）
4. AggregatedMetrics  - 综合聚合结果
5. MetricsAggregator  - 聚合器主体，实现 MetricsSink

设计要点：
- 实时聚合：每批次样本到来时增量更新，无需遍历历史数据
- 按标签分组：支持按请求名/步骤名/标签分组统计
- 时间分桶：按秒/分钟分桶，支持绘制时序曲线
- 线程安全：使用读写锁保护聚合状态
"""
from __future__ import annotations

import math
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from ..metrics.sample import Sample, SampleStatus, SampleType
from ..metrics.collector import MetricsSink
from .histogram import HdrHistogram, HistogramSnapshot


@dataclass
class PercentileMetrics:
    """延迟分位指标"""
    count: int = 0
    min_ms: float = 0.0
    max_ms: float = 0.0
    mean_ms: float = 0.0
    stddev_ms: float = 0.0
    p50_ms: float = 0.0
    p75_ms: float = 0.0
    p90_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    p999_ms: float = 0.0
    p9999_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "min_ms": round(self.min_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "mean_ms": round(self.mean_ms, 3),
            "stddev_ms": round(self.stddev_ms, 3),
            "p50_ms": round(self.p50_ms, 3),
            "p75_ms": round(self.p75_ms, 3),
            "p90_ms": round(self.p90_ms, 3),
            "p95_ms": round(self.p95_ms, 3),
            "p99_ms": round(self.p99_ms, 3),
            "p99.9_ms": round(self.p999_ms, 3),
            "p99.99_ms": round(self.p9999_ms, 3),
        }


@dataclass
class ThroughputMetrics:
    """吞吐量指标"""
    total_requests: int = 0
    total_success: int = 0
    total_failures: int = 0
    duration_seconds: float = 0.0
    overall_qps: float = 0.0
    success_qps: float = 0.0
    failure_qps: float = 0.0
    peak_qps: float = 0.0
    qps_series: List[Tuple[float, float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_requests": self.total_requests,
            "total_success": self.total_success,
            "total_failures": self.total_failures,
            "duration_seconds": round(self.duration_seconds, 2),
            "overall_qps": round(self.overall_qps, 2),
            "success_qps": round(self.success_qps, 2),
            "failure_qps": round(self.failure_qps, 2),
            "peak_qps": round(self.peak_qps, 2),
        }


@dataclass
class ErrorMetrics:
    """错误指标"""
    total_errors: int = 0
    error_rate: float = 0.0
    by_status_code: Dict[int, int] = field(default_factory=dict)
    by_error_type: Dict[str, int] = field(default_factory=dict)
    by_error_message: Dict[str, int] = field(default_factory=dict)
    top_errors: List[Tuple[str, int, float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_errors": self.total_errors,
            "error_rate_percent": round(self.error_rate * 100, 4),
            "by_status_code": {str(k): v for k, v in self.by_status_code.items()},
            "by_error_type": self.by_error_type,
            "top_errors": [
                {"message": msg, "count": cnt, "percent": round(pct * 100, 2)}
                for msg, cnt, pct in self.top_errors
            ],
        }


@dataclass
class AggregatedMetrics:
    """综合聚合指标"""
    start_time: float = 0.0
    end_time: float = 0.0
    duration_seconds: float = 0.0
    overall: PercentileMetrics = field(default_factory=PercentileMetrics)
    throughput: ThroughputMetrics = field(default_factory=ThroughputMetrics)
    errors: ErrorMetrics = field(default_factory=ErrorMetrics)
    by_name: Dict[str, PercentileMetrics] = field(default_factory=dict)
    by_status: Dict[str, int] = field(default_factory=dict)
    histogram_snapshot: Optional[HistogramSnapshot] = None

    def to_dict(self) -> dict:
        return {
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_seconds": round(self.duration_seconds, 2),
            "overall_latency": self.overall.to_dict(),
            "throughput": self.throughput.to_dict(),
            "errors": self.errors.to_dict(),
            "by_name": {k: v.to_dict() for k, v in self.by_name.items()},
            "by_status": {k.value if hasattr(k, "value") else k: v for k, v in self.by_status.items()},
        }


class MetricsAggregator(MetricsSink):
    """指标聚合器

    作为 MetricsSink 接入采集器，每批样本到来时实时增量聚合。

    内部状态：
    - _histogram: 全局 HDR 直方图，用于分位延迟
    - _name_histograms: 按名称分组的直方图
    - _success_count / _failure_count / _total_count: 原子计数
    - _time_buckets: 按秒分桶的计数（用于时序QPS）
    - _status_codes: 状态码分布
    - _error_types: 错误类型分布
    """

    DEFAULT_ERRORS_TO_KEEP = 100

    def __init__(
        self,
        histogram_lowest_ns: int = 1_000,
        histogram_highest_ns: int = 60_000_000_000,
        histogram_sig_digits: int = 3,
        time_bucket_seconds: int = 1,
        keep_time_buckets: int = 3600,
        top_errors_limit: int = 10,
    ):
        self._sig_digits = histogram_sig_digits
        self._hist_lo = histogram_lowest_ns
        self._hist_hi = histogram_highest_ns

        # 全局直方图
        self._histogram = HdrHistogram(
            lowest_value_ns=histogram_lowest_ns,
            highest_value_ns=histogram_highest_ns,
            significant_digits=histogram_sig_digits,
        )
        # 按名称分组的直方图
        self._name_histograms: Dict[str, HdrHistogram] = {}

        # 计数
        self._total_count = 0
        self._success_count = 0
        self._failure_count = 0

        # 状态分布
        self._status_counts: Dict[SampleStatus, int] = defaultdict(int)
        self._status_codes: Dict[int, int] = defaultdict(int)
        self._error_types: Dict[str, int] = defaultdict(int)
        self._error_messages: Dict[str, int] = defaultdict(int)

        # 时间分桶
        self._bucket_size = time_bucket_seconds
        self._keep_buckets = keep_time_buckets
        self._time_buckets: Deque[Tuple[float, int, int]] = deque(maxlen=keep_time_buckets)
        # (bucket_start_ts, total_in_bucket, success_in_bucket)
        self._current_bucket_ts: Optional[float] = None
        self._current_bucket_total = 0
        self._current_bucket_success = 0

        # 时间范围
        self._first_ts: Optional[float] = None
        self._last_ts: Optional[float] = None

        # 锁
        self._lock = threading.RLock()
        self._top_errors_limit = top_errors_limit

    # ============ MetricsSink 接口 ============
    def write(self, samples: List[Sample]) -> None:
        """写入一批样本，增量聚合"""
        if not samples:
            return

        with self._lock:
            for sample in samples:
                self._record_sample(sample)

    # ============ 核心记录逻辑 ============
    def _record_sample(self, sample: Sample) -> None:
        """记录单个样本（已加锁）"""
        # 1. 时间范围
        ts = sample.timestamp
        if self._first_ts is None or ts < self._first_ts:
            self._first_ts = ts
        if self._last_ts is None or ts > self._last_ts:
            self._last_ts = ts

        # 2. 延迟转换为纳秒
        latency_ns = int(sample.latency * 1_000_000_000)
        # 3. 全局直方图
        self._histogram.record_value(latency_ns)

        # 4. 按名称分组直方图
        name = sample.name or "unknown"
        if name not in self._name_histograms:
            self._name_histograms[name] = HdrHistogram(
                lowest_value_ns=self._hist_lo,
                highest_value_ns=self._hist_hi,
                significant_digits=self._sig_digits,
            )
        self._name_histograms[name].record_value(latency_ns)

        # 5. 计数
        self._total_count += 1
        if sample.is_success:
            self._success_count += 1
        else:
            self._failure_count += 1

        # 6. 状态分布
        self._status_counts[sample.status] += 1
        if sample.status_code:
            self._status_codes[sample.status_code] += 1

        # 7. 错误统计
        if sample.is_error:
            self._error_types[sample.status.value] += 1
            if sample.error_message:
                # 截断过长的错误信息
                key = sample.error_message[:200]
                self._error_messages[key] += 1

        # 8. 时间分桶（QPS 计算）
        bucket_ts = math.floor(ts / self._bucket_size) * self._bucket_size
        if self._current_bucket_ts is None:
            self._current_bucket_ts = bucket_ts
            self._current_bucket_total = 0
            self._current_bucket_success = 0
        elif bucket_ts != self._current_bucket_ts:
            # 上一个桶结束，入队
            self._time_buckets.append((
                self._current_bucket_ts,
                self._current_bucket_total,
                self._current_bucket_success,
            ))
            self._current_bucket_ts = bucket_ts
            self._current_bucket_total = 0
            self._current_bucket_success = 0

        self._current_bucket_total += 1
        if sample.is_success:
            self._current_bucket_success += 1

    # ============ 结果输出 ============
    def build(self) -> AggregatedMetrics:
        """构建综合聚合结果"""
        with self._lock:
            # 将当前桶也入队用于计算
            if self._current_bucket_ts is not None and self._current_bucket_total > 0:
                self._time_buckets.append((
                    self._current_bucket_ts,
                    self._current_bucket_total,
                    self._current_bucket_success,
                ))

            now = time.time()
            start = self._first_ts or now
            end = self._last_ts or now
            duration = max(0.001, end - start)

            # 1. 整体延迟
            overall = self._histogram_to_percentile_metrics(self._histogram)

            # 2. 吞吐量
            throughput = self._build_throughput(duration)

            # 3. 错误
            errors = self._build_errors()

            # 4. 按名称分组
            by_name = {
                name: self._histogram_to_percentile_metrics(hist)
                for name, hist in self._name_histograms.items()
            }

            # 5. 状态分布（转普通dict）
            by_status = {k.value: v for k, v in self._status_counts.items()}

            metrics = AggregatedMetrics(
                start_time=start,
                end_time=end,
                duration_seconds=duration,
                overall=overall,
                throughput=throughput,
                errors=errors,
                by_name=by_name,
                by_status=by_status,
                histogram_snapshot=self._histogram.snapshot(),
            )

            # 弹出刚才入队的当前桶
            if self._current_bucket_ts is not None and self._current_bucket_total > 0:
                if self._time_buckets:
                    self._time_buckets.pop()

            return metrics

    def _histogram_to_percentile_metrics(self, hist: HdrHistogram) -> PercentileMetrics:
        """将直方图转换为 PercentileMetrics"""
        m = PercentileMetrics()
        if hist.total_count == 0:
            return m

        snap = hist.snapshot()
        pcts = snap.percentiles

        ns_to_ms = 1.0 / 1_000_000
        m.count = snap.count
        m.min_ms = snap.min_value * ns_to_ms
        m.max_ms = snap.max_value * ns_to_ms
        m.mean_ms = snap.mean * ns_to_ms
        m.stddev_ms = snap.stddev * ns_to_ms
        m.p50_ms = pcts.get("p50", 0) * ns_to_ms
        m.p75_ms = pcts.get("p75", 0) * ns_to_ms
        m.p90_ms = pcts.get("p90", 0) * ns_to_ms
        m.p95_ms = pcts.get("p95", 0) * ns_to_ms
        m.p99_ms = pcts.get("p99", 0) * ns_to_ms
        m.p999_ms = pcts.get("p99.9", 0) * ns_to_ms
        m.p9999_ms = pcts.get("p99.99", 0) * ns_to_ms
        return m

    def _build_throughput(self, duration: float) -> ThroughputMetrics:
        t = ThroughputMetrics()
        t.total_requests = self._total_count
        t.total_success = self._success_count
        t.total_failures = self._failure_count
        t.duration_seconds = duration
        t.overall_qps = self._total_count / duration if duration > 0 else 0
        t.success_qps = self._success_count / duration if duration > 0 else 0
        t.failure_qps = self._failure_count / duration if duration > 0 else 0

        # 计算峰值QPS和时序
        peak = 0.0
        qps_series: List[Tuple[float, float]] = []
        for bucket_ts, total, _success in self._time_buckets:
            qps = total / self._bucket_size
            if qps > peak:
                peak = qps
            qps_series.append((bucket_ts, qps))
        # 当前桶也算
        if self._current_bucket_ts is not None and self._current_bucket_total > 0:
            qps = self._current_bucket_total / self._bucket_size
            if qps > peak:
                peak = qps
            qps_series.append((self._current_bucket_ts, qps))

        t.peak_qps = peak
        t.qps_series = qps_series
        return t

    def _build_errors(self) -> ErrorMetrics:
        e = ErrorMetrics()
        e.total_errors = self._failure_count
        e.error_rate = (
            self._failure_count / self._total_count
            if self._total_count > 0 else 0.0
        )
        e.by_status_code = dict(self._status_codes)
        e.by_error_type = dict(self._error_types)

        # Top errors
        if self._failure_count > 0:
            sorted_errors = sorted(
                self._error_messages.items(),
                key=lambda x: x[1],
                reverse=True,
            )[: self._top_errors_limit]
            e.top_errors = [
                (msg, cnt, cnt / self._total_count)
                for msg, cnt in sorted_errors
            ]
            e.by_error_message = {
                k: v for k, v in sorted_errors
            }
        return e

    # ============ 实时监控支持 ============
    def get_realtime_stats(self, window_seconds: float = 10.0) -> Dict[str, Any]:
        """获取最近窗口内的实时指标（用于进度显示）"""
        with self._lock:
            if self._current_bucket_ts is None:
                return {
                    "total": 0, "qps": 0.0, "success": 0,
                    "errors": 0, "error_rate": 0.0,
                    "avg_latency_ms": 0.0, "p95_latency_ms": 0.0,
                }

            # 收集最近 N 个桶
            cutoff_ts = self._current_bucket_ts - window_seconds + self._bucket_size
            total = 0
            success = 0
            for bucket_ts, b_total, b_success in reversed(self._time_buckets):
                if bucket_ts < cutoff_ts:
                    break
                total += b_total
                success += b_success
            # 加上当前桶
            if self._current_bucket_ts >= cutoff_ts:
                total += self._current_bucket_total
                success += self._current_bucket_success

            error_rate = (total - success) / total if total > 0 else 0.0
            avg_lat = self._histogram.get_mean() / 1_000_000
            p95_lat = self._histogram.get_value_at_percentile(95.0) / 1_000_000

            return {
                "total": self._total_count,
                "window_total": total,
                "qps": total / window_seconds if window_seconds > 0 else 0.0,
                "success": self._success_count,
                "errors": self._failure_count,
                "error_rate": error_rate,
                "avg_latency_ms": round(avg_lat, 2),
                "p95_latency_ms": round(p95_lat, 2),
                "active_buckets": min(len(self._time_buckets) + 1,
                                       int(window_seconds / self._bucket_size)),
            }

    def reset(self) -> None:
        """清空所有聚合数据"""
        with self._lock:
            self._histogram.reset()
            self._name_histograms.clear()
            self._total_count = 0
            self._success_count = 0
            self._failure_count = 0
            self._status_counts.clear()
            self._status_codes.clear()
            self._error_types.clear()
            self._error_messages.clear()
            self._time_buckets.clear()
            self._current_bucket_ts = None
            self._current_bucket_total = 0
            self._current_bucket_success = 0
            self._first_ts = None
            self._last_ts = None
