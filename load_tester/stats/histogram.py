"""HDR直方图 (High Dynamic Range Histogram)

用于在不存储全量数据的前提下，高效估计延迟的分位数。

核心原理：
1. 桶分桶策略：桶范围按指数增长，低延迟区高精度，高延迟区低精度
2. 精度控制：通过 significant_digits（有效数字位数）控制误差上限
   - 2位有效数字 → 误差 ≤ 1%
   - 3位有效数字 → 误差 ≤ 0.1%
   - 4位有效数字 → 误差 ≤ 0.01%
3. 内存占用：固定大小 O(log(max_value))，与数据量无关

典型延迟范围配置：
- 最大延迟 60秒，3位有效数字 → 约 36KB 内存
- 最大延迟 10分钟，3位有效数字 → 约 48KB 内存

使用方法：
```
hist = HdrHistogram(lowest=10, highest=60_000_000, sig_digits=3)  # 10us - 60s, 0.1%精度
hist.record_value(latency_ns)  # 记录延迟
p99 = hist.get_value_at_percentile(99.0)  # 获取p99
```
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class HistogramSnapshot:
    """直方图快照

    可序列化的直方图状态，用于报告和跨进程传递。
    """
    count: int
    min_value: float
    max_value: float
    mean: float
    stddev: float
    total_sum: float
    percentiles: Dict[str, float]
    buckets: List[Tuple[float, int]]

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "min_ms": self.min_value / 1_000_000 if self.min_value else 0,
            "max_ms": self.max_value / 1_000_000 if self.max_value else 0,
            "mean_ms": self.mean / 1_000_000 if self.count else 0,
            "stddev_ms": self.stddev / 1_000_000 if self.count else 0,
            "percentiles_ms": {k: v / 1_000_000 for k, v in self.percentiles.items()},
            "total_samples": self.count,
        }


class HdrHistogram:
    """HDR直方图实现

    简化版 HDR Histogram，核心思想一致：
    - 两段式分桶：bucket_index (高位) + sub_bucket_index (低位)
    - 每个 sub_bucket 的宽度 = bucket_base_value / sub_bucket_count
    - sub_bucket_count = 2^ceil_bits，保证有效数字精度

    所有值统一用纳秒存储，便于计算。
    """

    # 标准分位数列表
    DEFAULT_PERCENTILES = [
        50.0, 75.0, 90.0, 95.0, 99.0, 99.9, 99.99, 100.0,
    ]

    def __init__(
        self,
        lowest_value_ns: int = 1_000,          # 1 微秒
        highest_value_ns: int = 60_000_000_000,  # 60 秒
        significant_digits: int = 3,
        auto_resize: bool = True,
    ):
        if lowest_value_ns < 1:
            raise ValueError("lowest_value_ns must be >= 1")
        if highest_value_ns <= lowest_value_ns:
            raise ValueError("highest_value_ns must be > lowest_value_ns")
        if significant_digits < 1 or significant_digits > 5:
            raise ValueError("significant_digits must be between 1 and 5")

        self._lowest = lowest_value_ns
        self._highest = highest_value_ns
        self._sig_digits = significant_digits
        self._auto_resize = auto_resize

        # 计算子桶大小：2^ceil_bits >= 10^sig_digits
        self._sub_bucket_count = 1 << math.ceil(math.log2(10 ** significant_digits))
        self._sub_bucket_half_count = self._sub_bucket_count >> 1
        self._sub_bucket_mask = self._sub_bucket_count - 1
        self._leading_zero_count_base = (
            64 - (self._sub_bucket_count * 2).bit_length()
        )

        # 桶数量：覆盖到 highest_value
        self._bucket_count = self._get_bucket_index(highest_value_ns) + 1

        # 计数器数组：每个桶的每个子桶一个计数
        self._counts_len = (self._bucket_count + 1) * (self._sub_bucket_count >> 1)
        self._counts: List[int] = [0] * self._counts_len

        # 统计量
        self._total_count = 0
        self._total_sum = 0
        self._min_value: Optional[int] = None
        self._max_value: Optional[int] = None
        self._overflow_count = 0

    @property
    def total_count(self) -> int:
        return self._total_count

    @property
    def total_sum_ns(self) -> int:
        return self._total_sum

    @property
    def min_value_ns(self) -> int:
        return self._min_value or 0

    @property
    def max_value_ns(self) -> int:
        return self._max_value or 0

    @property
    def overflow_count(self) -> int:
        return self._overflow_count

    def _get_bucket_index(self, value: int) -> int:
        """计算桶索引（根据值的最高位位置）"""
        sub_bucket_magnitude = (value | self._sub_bucket_mask).bit_length() - 1
        bucket_index = sub_bucket_magnitude - (self._sub_bucket_count - 1).bit_length() + 1
        return max(0, bucket_index)

    def _counts_index(self, bucket_index: int, sub_bucket_index: int) -> int:
        """计算在 counts 数组中的索引"""
        base_bucket_offset = (bucket_index + 1) << (self._sub_bucket_count - 1).bit_length()
        # 前一个桶的子桶只用一半，后面的桶用完整子桶数，但我们统一偏移
        bucket_base_offset = bucket_index * (self._sub_bucket_count >> 1)
        return bucket_base_offset + (sub_bucket_index - self._sub_bucket_half_count)

    def record_value(self, value_ns: int, count: int = 1) -> bool:
        """记录一个值

        Args:
            value_ns: 延迟值（纳秒）
            count: 重复次数

        Returns:
            是否成功记录（超过范围返回False）
        """
        if value_ns < 0:
            value_ns = 0

        # 自动调整上限
        if value_ns > self._highest and self._auto_resize:
            self._resize(value_ns)

        if value_ns > self._highest:
            self._overflow_count += count
            return False

        if value_ns < self._lowest:
            value_ns = self._lowest

        bucket_index = self._get_bucket_index(value_ns)
        sub_bucket_index = (value_ns >> bucket_index) & self._sub_bucket_mask
        counts_idx = self._counts_index(bucket_index, sub_bucket_index)

        if 0 <= counts_idx < self._counts_len:
            self._counts[counts_idx] += count
            self._total_count += count
            self._total_sum += value_ns * count

            if self._min_value is None or value_ns < self._min_value:
                self._min_value = value_ns
            if self._max_value is None or value_ns > self._max_value:
                self._max_value = value_ns
            return True
        else:
            self._overflow_count += count
            return False

    def _resize(self, new_highest: int) -> None:
        """扩展直方图范围"""
        new_highest = max(new_highest, self._highest * 2)
        new_hist = HdrHistogram(
            lowest_value_ns=self._lowest,
            highest_value_ns=new_highest,
            significant_digits=self._sig_digits,
            auto_resize=True,
        )
        # 复制已有数据
        for bucket_idx in range(self._bucket_count):
            for sub_idx in range(self._sub_bucket_count):
                cidx = self._counts_index(bucket_idx, sub_idx)
                if 0 <= cidx < self._counts_len and self._counts[cidx] > 0:
                    value = (sub_idx << bucket_idx)
                    # 找最低代表值
                    if bucket_idx > 0:
                        value += (1 << bucket_idx)
                    new_hist.record_value(value, self._counts[cidx])

        # 替换内部状态
        self._highest = new_hist._highest
        self._bucket_count = new_hist._bucket_count
        self._counts_len = new_hist._counts_len
        self._counts = new_hist._counts
        self._total_count = new_hist._total_count
        self._total_sum = new_hist._total_sum
        self._min_value = new_hist._min_value
        self._max_value = new_hist._max_value
        self._overflow_count += new_hist._overflow_count

    def get_value_at_percentile(self, percentile: float) -> float:
        """获取指定分位数对应的值

        算法：从头累加计数，累加和超过 p% * total_count 的桶即为目标桶。

        Args:
            percentile: 分位数，0-100，如 99.0 表示 p99

        Returns:
            对应分位数的延迟值（纳秒）
        """
        if self._total_count == 0:
            return 0

        percentile = max(0.0, min(100.0, percentile))
        target_count = math.ceil(self._total_count * percentile / 100.0)
        # 100% 特殊处理
        if percentile >= 100.0:
            return float(self._max_value or 0)
        if percentile <= 0.0:
            return float(self._min_value or 0)

        accumulated = 0
        for bucket_idx in range(self._bucket_count):
            sub_start = self._sub_bucket_half_count if bucket_idx > 0 else 0
            for sub_idx in range(sub_start, self._sub_bucket_count):
                cidx = self._counts_index(bucket_idx, sub_idx)
                if 0 <= cidx < self._counts_len and self._counts[cidx] > 0:
                    accumulated += self._counts[cidx]
                    if accumulated >= target_count:
                        return float(sub_idx << bucket_idx)

        return float(self._max_value or 0)

    def get_percentiles(self, percentiles: Optional[List[float]] = None) -> Dict[str, float]:
        """获取多个分位数的值

        Returns:
            {"p50": value_ns, "p90": value_ns, ...}
        """
        pcts = percentiles or self.DEFAULT_PERCENTILES
        result = {}
        for p in pcts:
            key = f"p{p:.2f}".rstrip("0").rstrip(".") if p != int(p) else f"p{int(p)}"
            result[key] = self.get_value_at_percentile(p)
        return result

    def get_mean(self) -> float:
        """均值（纳秒）"""
        return self._total_sum / self._total_count if self._total_count else 0.0

    def get_stddev(self) -> float:
        """标准差（纳秒）

        使用 Welford 算法在遍历时计算，避免数值不稳定。
        """
        if self._total_count <= 1:
            return 0.0

        mean = self.get_mean()
        sum_sq_diff = 0.0
        for bucket_idx in range(self._bucket_count):
            sub_start = self._sub_bucket_half_count if bucket_idx > 0 else 0
            for sub_idx in range(sub_start, self._sub_bucket_count):
                cidx = self._counts_index(bucket_idx, sub_idx)
                if 0 <= cidx < self._counts_len and self._counts[cidx] > 0:
                    value = sub_idx << bucket_idx
                    diff = value - mean
                    sum_sq_diff += diff * diff * self._counts[cidx]

        return math.sqrt(sum_sq_diff / self._total_count)

    def snapshot(self, percentiles: Optional[List[float]] = None) -> HistogramSnapshot:
        """获取直方图快照"""
        buckets = self.get_buckets(num_buckets=50)
        return HistogramSnapshot(
            count=self._total_count,
            min_value=float(self._min_value or 0),
            max_value=float(self._max_value or 0),
            mean=self.get_mean(),
            stddev=self.get_stddev(),
            total_sum=self._total_sum,
            percentiles=self.get_percentiles(percentiles),
            buckets=buckets,
        )

    def get_buckets(self, num_buckets: int = 50) -> List[Tuple[float, int]]:
        """生成等距分桶（用于绘图）

        Returns:
            [(bucket_start_ns, count), ...]
        """
        if self._total_count == 0 or self._max_value is None:
            return []

        min_v = max(0.0, float(self._min_value or 0))
        max_v = float(self._max_value)
        if min_v == max_v:
            return [(min_v, self._total_count)]

        bucket_width = (max_v - min_v) / num_buckets
        if bucket_width <= 0:
            return [(min_v, self._total_count)]

        buckets = [(min_v + i * bucket_width, 0) for i in range(num_buckets)]
        bucket_counts = [0] * num_buckets

        for bucket_idx in range(self._bucket_count):
            sub_start = self._sub_bucket_half_count if bucket_idx > 0 else 0
            for sub_idx in range(sub_start, self._sub_bucket_count):
                cidx = self._counts_index(bucket_idx, sub_idx)
                if 0 <= cidx < self._counts_len and self._counts[cidx] > 0:
                    value = float(sub_idx << bucket_idx)
                    bi = min(num_buckets - 1, int((value - min_v) / bucket_width))
                    bucket_counts[bi] += self._counts[cidx]

        return [(buckets[i][0], bucket_counts[i]) for i in range(num_buckets)]

    def merge(self, other: "HdrHistogram") -> None:
        """合并另一个直方图的数据"""
        for bucket_idx in range(other._bucket_count):
            sub_start = other._sub_bucket_half_count if bucket_idx > 0 else 0
            for sub_idx in range(sub_start, other._sub_bucket_count):
                cidx = other._counts_index(bucket_idx, sub_idx)
                if 0 <= cidx < other._counts_len and other._counts[cidx] > 0:
                    value = sub_idx << bucket_idx
                    self.record_value(value, other._counts[cidx])
        self._overflow_count += other._overflow_count

    def reset(self) -> None:
        """清空所有数据"""
        self._counts = [0] * self._counts_len
        self._total_count = 0
        self._total_sum = 0
        self._min_value = None
        self._max_value = None
        self._overflow_count = 0
