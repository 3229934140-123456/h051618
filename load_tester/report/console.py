"""控制台报告输出器

以美观的ASCII表格形式输出压测结果。
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from typing import Dict, List, Optional, TextIO

from ..stats.aggregator import AggregatedMetrics, PercentileMetrics


def _fmt_ms(value: float) -> str:
    """格式化毫秒值"""
    if value >= 1000:
        return f"{value/1000:.3f}s"
    elif value >= 1:
        return f"{value:.2f}ms"
    elif value >= 0.001:
        return f"{value*1000:.2f}us"
    else:
        return f"{value*1_000_000:.1f}ns"


def _fmt_qps(value: float) -> str:
    """格式化QPS"""
    if value >= 1_000_000:
        return f"{value/1_000_000:.2f}M/s"
    elif value >= 1_000:
        return f"{value/1_000:.2f}K/s"
    else:
        return f"{value:.2f}/s"


def _fmt_pct(value: float) -> str:
    """格式化百分比"""
    return f"{value*100:.4f}%" if value < 0.01 else f"{value*100:.2f}%"


def _draw_progress_bar(pct: float, width: int = 30) -> str:
    """绘制简单的进度条"""
    filled = int(pct * width)
    return "█" * filled + "░" * (width - filled)


class ConsoleReporter:
    """控制台报告生成器

    输出格式：
    - 顶部：压测概要（时间、总请求数、成功率）
    - 延迟分位表：p50/p75/p90/p95/p99/p99.9 等
    - 吞吐量表：QPS、峰值QPS、总请求
    - 错误统计表：按状态码、按错误类型
    - 分组明细表：按请求名分组的延迟统计
    """

    def __init__(
        self,
        output: Optional[TextIO] = None,
        color: bool = True,
        show_progress: bool = True,
        show_by_name: bool = True,
        show_errors: bool = True,
    ):
        self._output = output or sys.stdout
        self._color = color
        self._show_progress = show_progress
        self._show_by_name = show_by_name
        self._show_errors = show_errors

    # ANSI 颜色
    class C:
        RESET = "\033[0m"
        BOLD = "\033[1m"
        RED = "\033[31m"
        GREEN = "\033[32m"
        YELLOW = "\033[33m"
        BLUE = "\033[34m"
        MAGENTA = "\033[35m"
        CYAN = "\033[36m"
        GRAY = "\033[90m"

    def _c(self, text: str, color: str) -> str:
        if self._color:
            return f"{color}{text}{self.C.RESET}"
        return text

    def report_progress(self, progress: float, state: dict, realtime_stats: dict) -> None:
        """输出实时进度条"""
        if not self._show_progress:
            return

        bar = _draw_progress_bar(min(1.0, progress / 100.0))
        phase = state.get("phase", "running")
        workers = state.get("active_workers", 0)
        qps = realtime_stats.get("qps", 0)
        total = realtime_stats.get("total", 0)
        err_rate = realtime_stats.get("error_rate", 0)
        p95 = realtime_stats.get("p95_latency_ms", 0)

        phase_color = self.C.GREEN if phase in ("steady", "hold") else (
            self.C.YELLOW if phase.startswith("step") or phase == "ramp" else self.C.CYAN
        )
        err_color = self.C.RED if err_rate > 0.05 else (self.C.YELLOW if err_rate > 0.01 else self.C.GREEN)

        line = (
            f"\r  [{bar}] {progress:5.1f}% | "
            f"Phase: {self._c(phase, phase_color):<16} | "
            f"Workers: {workers:<5} | "
            f"QPS: {_fmt_qps(qps):<10} | "
            f"Total: {total:>8} | "
            f"P95: {_fmt_ms(p95):<10} | "
            f"Err: {self._c(_fmt_pct(err_rate), err_color)}"
        )
        self._output.write(line)
        self._output.flush()

    def report(self, metrics: AggregatedMetrics, title: str = "Load Test Report") -> str:
        """生成完整报告并输出"""
        buf = StringIO()
        self._write(buf, metrics, title)
        content = buf.getvalue()
        self._output.write(content)
        self._output.write("\n")
        self._output.flush()
        return content

    def _write(self, buf: StringIO, m: AggregatedMetrics, title: str) -> None:
        self._section_title(buf, f"=== {title} ===")

        # 1. 概要
        self._section_title(buf, "SUMMARY")
        start_dt = datetime.fromtimestamp(m.start_time).strftime("%Y-%m-%d %H:%M:%S")
        end_dt = datetime.fromtimestamp(m.end_time).strftime("%Y-%m-%d %H:%M:%S")
        duration = m.duration_seconds
        total = m.throughput.total_requests
        success = m.throughput.total_success
        failures = m.throughput.total_failures
        success_rate = success / total if total > 0 else 1.0

        lines = [
            ("Start Time", start_dt),
            ("End Time", end_dt),
            ("Duration", f"{duration:.2f}s ({duration/60:.2f}min)"),
            ("Total Requests", str(total)),
            ("Successful", self._c(str(success), self.C.GREEN)),
            ("Failed", self._c(str(failures), self.C.RED if failures > 0 else self.C.GREEN)),
            ("Success Rate", self._c(_fmt_pct(success_rate),
                 self.C.GREEN if success_rate >= 0.99 else (
                 self.C.YELLOW if success_rate >= 0.95 else self.C.RED))),
        ]
        self._kv_table(buf, lines)

        # 2. 延迟分位
        self._section_title(buf, "LATENCY DISTRIBUTION")
        lat = m.overall
        lat_lines = [
            ("Count", str(lat.count)),
            ("Min", _fmt_ms(lat.min_ms)),
            ("p50", _fmt_ms(lat.p50_ms)),
            ("p75", _fmt_ms(lat.p75_ms)),
            ("p90", _fmt_ms(lat.p90_ms)),
            ("p95", _fmt_ms(lat.p95_ms)),
            ("p99", _fmt_ms(lat.p99_ms)),
            ("p99.9", _fmt_ms(lat.p999_ms)),
            ("p99.99", _fmt_ms(lat.p9999_ms)),
            ("Max", _fmt_ms(lat.max_ms)),
            ("Mean", _fmt_ms(lat.mean_ms)),
            ("StdDev", _fmt_ms(lat.stddev_ms)),
        ]
        self._kv_table(buf, lat_lines)

        # 延迟分布可视化
        self._draw_latency_bars(buf, m)

        # 3. 吞吐量
        self._section_title(buf, "THROUGHPUT")
        t = m.throughput
        tp_lines = [
            ("Overall QPS", _fmt_qps(t.overall_qps)),
            ("Success QPS", _fmt_qps(t.success_qps)),
            ("Failure QPS", _fmt_qps(t.failure_qps)),
            ("Peak QPS", self._c(_fmt_qps(t.peak_qps), self.C.MAGENTA)),
            ("Avg per Second", _fmt_qps(t.total_requests / max(0.001, t.duration_seconds))),
        ]
        self._kv_table(buf, tp_lines)

        # 4. 错误统计
        if self._show_errors and m.errors.total_errors > 0:
            self._section_title(buf, "ERRORS")
            e = m.errors
            err_lines = [
                ("Total Errors", str(e.total_errors)),
                ("Error Rate", self._c(_fmt_pct(e.error_rate),
                     self.C.RED if e.error_rate > 0.05 else self.C.YELLOW)),
            ]
            self._kv_table(buf, err_lines)

            if e.by_status_code:
                self._subsection(buf, "By Status Code")
                sc_items = sorted(e.by_status_code.items(), key=lambda x: -x[1])
                sc_lines = [(str(k), str(v), _fmt_pct(v / t.total_requests if t.total_requests else 0))
                           for k, v in sc_items]
                self._3col_table(buf, ["Status Code", "Count", "Pct"], sc_lines)

            if e.top_errors:
                self._subsection(buf, f"Top {len(e.top_errors)} Errors")
                err_items = [
                    (msg[:60] + "..." if len(msg) > 63 else msg,
                     str(cnt), _fmt_pct(pct))
                    for msg, cnt, pct in e.top_errors
                ]
                self._3col_table(buf, ["Message", "Count", "Pct"], err_items)

        # 5. 按名称分组统计
        if self._show_by_name and m.by_name:
            self._section_title(buf, "PER REQUEST BREAKDOWN")
            self._by_name_table(buf, m)

    def _section_title(self, buf: StringIO, title: str) -> None:
        buf.write("\n")
        buf.write(self._c(title, self.C.BOLD + self.C.BLUE))
        buf.write("\n")

    def _subsection(self, buf: StringIO, title: str) -> None:
        buf.write("\n")
        buf.write(self._c(f"  -- {title} --", self.C.CYAN))
        buf.write("\n")

    def _kv_table(self, buf: StringIO, pairs: list) -> None:
        max_k = max(len(str(k)) for k, _ in pairs) if pairs else 10
        for k, v in pairs:
            buf.write(f"  {k:<{max_k}} : {v}\n")

    def _3col_table(self, buf: StringIO, headers: list, rows: list) -> None:
        if not rows:
            return
        widths = [max(len(headers[i]), max(len(str(r[i])) for r in rows)) for i in range(3)]
        sep = "+" + "+".join(["-" * (w + 2) for w in widths]) + "+"
        buf.write("  ")
        buf.write(sep)
        buf.write("\n  |")
        for i, h in enumerate(headers):
            buf.write(f" {self._c(h, self.C.BOLD):<{widths[i]}} |")
        buf.write("\n  ")
        buf.write(sep.replace("-", "="))
        buf.write("\n")
        for row in rows:
            buf.write("  |")
            for i, v in enumerate(row):
                buf.write(f" {str(v):<{widths[i]}} |")
            buf.write("\n")
        buf.write("  ")
        buf.write(sep)
        buf.write("\n")

    def _by_name_table(self, buf: StringIO, m: AggregatedMetrics) -> None:
        headers = ["Name", "Count", "P50", "P90", "P99", "P99.9", "Mean", "Err%"]
        rows = []
        for name, lat in sorted(m.by_name.items()):
            err_pct = "N/A"
            total_for_name = lat.count
            if total_for_name > 0 and m.throughput.total_requests > 0:
                # 估算每个name的错误数（通过总错误率）
                pass
            rows.append([
                name[:30] + ("..." if len(name) > 30 else ""),
                str(lat.count),
                _fmt_ms(lat.p50_ms),
                _fmt_ms(lat.p90_ms),
                _fmt_ms(lat.p99_ms),
                _fmt_ms(lat.p999_ms),
                _fmt_ms(lat.mean_ms),
                _fmt_pct(m.errors.error_rate) if lat.count > 0 else "N/A",
            ])
        if rows:
            widths = [max(len(headers[i]), max(len(r[i]) for r in rows)) for i in range(8)]
            sep = "+" + "+".join(["-" * (w + 2) for w in widths]) + "+"
            buf.write("  ")
            buf.write(sep)
            buf.write("\n  |")
            for i, h in enumerate(headers):
                buf.write(f" {self._c(h, self.C.BOLD):<{widths[i]}} |")
            buf.write("\n  ")
            buf.write(sep.replace("-", "="))
            buf.write("\n")
            for row in rows:
                buf.write("  |")
                for i, v in enumerate(row):
                    color = self.C.RED if i == 7 and "%" in v and float(v.rstrip('%')) > 5 else ""
                    colored = self._c(v, color) if color else v
                    buf.write(f" {colored:<{widths[i]}} |")
                buf.write("\n")
            buf.write("  ")
            buf.write(sep)
            buf.write("\n")

    def _draw_latency_bars(self, buf: StringIO, m: AggregatedMetrics) -> None:
        """绘制延迟分布的ASCII柱状图"""
        snap = m.histogram_snapshot
        if not snap or not snap.buckets:
            return

        buckets = snap.buckets
        if not buckets:
            return

        max_count = max(c for _, c in buckets) if buckets else 0
        if max_count == 0:
            return

        bar_width = 40
        self._subsection(buf, "Latency Distribution (approx)")
        buf.write("\n")
        for bucket_start, count in buckets:
            if count == 0:
                continue
            ms_val = bucket_start / 1_000_000
            width = int(count / max_count * bar_width)
            bar = "█" * max(1, width)
            buf.write(f"  {_fmt_ms(ms_val):>12} | {bar:<{bar_width}} {count}\n")
        buf.write("\n")
