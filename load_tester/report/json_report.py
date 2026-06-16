"""JSON报告输出器

将聚合结果输出为结构化的JSON文件，便于后续分析、CI集成等。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Union

from ..stats.aggregator import AggregatedMetrics


class JsonReporter:
    """JSON报告生成器

    输出结构：
    {
        "meta": { 报告元数据：时间、版本等 },
        "summary": { 核心指标：总请求、成功率、平均延迟、p99等 },
        "latency": { 完整的延迟分布 },
        "throughput": { 吞吐量和QPS时序 },
        "errors": { 错误统计 },
        "by_name": { 按请求名分组 },
        "histogram": { 直方图原始桶数据（用于绘图） }
    }
    """

    def __init__(self, pretty: bool = True, include_histogram: bool = True):
        self._pretty = pretty
        self._include_histogram = include_histogram

    def report(
        self,
        metrics: AggregatedMetrics,
        output_path: Optional[Union[str, Path]] = None,
        title: str = "Load Test Report",
        extra_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """生成JSON报告

        Args:
            metrics: 聚合指标
            output_path: 输出文件路径（None 则只返回字典）
            title: 报告标题
            extra_meta: 额外元数据

        Returns:
            完整的报告字典
        """
        report = self._build_report(metrics, title, extra_meta)

        if output_path:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                if self._pretty:
                    json.dump(report, f, ensure_ascii=False, indent=2)
                else:
                    json.dump(report, f, ensure_ascii=False, separators=(",", ":"))

        return report

    def _build_report(
        self,
        m: AggregatedMetrics,
        title: str,
        extra_meta: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        now = time.time()
        total = m.throughput.total_requests
        success = m.throughput.total_success
        errors = m.errors.total_errors

        summary = {
            "total_requests": total,
            "successful_requests": success,
            "failed_requests": errors,
            "success_rate": round(success / total, 6) if total > 0 else 1.0,
            "error_rate": round(m.errors.error_rate, 6),
            "duration_seconds": round(m.duration_seconds, 3),
            "start_timestamp": m.start_time,
            "end_timestamp": m.end_time,
            "start_datetime": datetime.fromtimestamp(m.start_time).isoformat(),
            "end_datetime": datetime.fromtimestamp(m.end_time).isoformat(),
        }

        # 延迟摘要
        latency_summary = {
            "mean_ms": round(m.overall.mean_ms, 3),
            "min_ms": round(m.overall.min_ms, 3),
            "max_ms": round(m.overall.max_ms, 3),
            "stddev_ms": round(m.overall.stddev_ms, 3),
            "p50_ms": round(m.overall.p50_ms, 3),
            "p75_ms": round(m.overall.p75_ms, 3),
            "p90_ms": round(m.overall.p90_ms, 3),
            "p95_ms": round(m.overall.p95_ms, 3),
            "p99_ms": round(m.overall.p99_ms, 3),
            "p999_ms": round(m.overall.p999_ms, 3),
            "p9999_ms": round(m.overall.p9999_ms, 3),
            "sample_count": m.overall.count,
        }

        # 吞吐量（含时序）
        throughput = {
            "overall_qps": round(m.throughput.overall_qps, 2),
            "success_qps": round(m.throughput.success_qps, 2),
            "failure_qps": round(m.throughput.failure_qps, 2),
            "peak_qps": round(m.throughput.peak_qps, 2),
            "duration_seconds": round(m.throughput.duration_seconds, 2),
            "qps_timeseries": [
                {"timestamp": ts, "qps": round(qps, 2)}
                for ts, qps in m.throughput.qps_series
            ],
        }

        # 错误
        errors = {
            "total": m.errors.total_errors,
            "rate": round(m.errors.error_rate, 6),
            "by_status_code": {str(k): v for k, v in m.errors.by_status_code.items()},
            "by_error_type": m.errors.by_error_type,
            "top_errors": [
                {"message": msg, "count": cnt, "percent": round(pct * 100, 4)}
                for msg, cnt, pct in m.errors.top_errors
            ],
        }

        # 按名称分组
        by_name = {
            name: lat.to_dict()
            for name, lat in m.by_name.items()
        }

        # 状态分布
        by_status = m.by_status

        # 直方图
        histogram = None
        if self._include_histogram and m.histogram_snapshot:
            snap = m.histogram_snapshot
            # 转换为更紧凑的格式
            histogram = {
                "total_count": snap.count,
                "min_ns": snap.min_value,
                "max_ns": snap.max_value,
                "mean_ns": snap.mean,
                "stddev_ns": snap.stddev,
                "percentiles_ns": {k: round(v, 3) for k, v in snap.percentiles.items()},
                "buckets": [
                    {"start_ns": round(bs, 3), "count": cnt}
                    for bs, cnt in snap.buckets
                ],
            }

        report = {
            "meta": {
                "title": title,
                "generated_at": now,
                "generated_at_datetime": datetime.fromtimestamp(now).isoformat(),
                "generator": "load_tester",
                "version": "1.0.0",
                **(extra_meta or {}),
            },
            "summary": summary,
            "latency": latency_summary,
            "throughput": throughput,
            "errors": errors,
            "by_name": by_name,
            "by_status": by_status,
        }
        if histogram:
            report["histogram"] = histogram

        return report
