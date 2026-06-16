"""采样数据结构

定义压测过程中每个请求/步骤产生的原始样本数据。
这些样本是后续统计聚合的输入。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SampleType(str, Enum):
    """采样类型"""
    REQUEST = "request"
    SCENARIO = "scenario"
    STEP = "step"
    CUSTOM = "custom"


class SampleStatus(str, Enum):
    """采样结果状态"""
    SUCCESS = "success"
    FAILURE = "failure"
    ERROR = "error"
    TIMEOUT = "timeout"
    ASSERTION_FAILED = "assertion_failed"


@dataclass
class Sample:
    """单个样本数据

    记录一次请求/步骤/场景的完整执行信息。
    设计为轻量级结构，便于高性能采集（百万级/秒）。

    关键字段：
    - latency: 端到端延迟（秒），使用 perf_counter() 保证高精度
    - status: 结果状态（成功/失败/错误/超时/断言失败）
    - tags: 标签，用于分组聚合（如按请求名、按错误类型等）
    - labels: 额外标签，KV形式，灵活扩展
    - status_code: HTTP状态码（如果适用）
    - error_message: 错误信息（如果失败）
    """

    name: str
    latency: float
    status: SampleStatus
    sample_type: SampleType = SampleType.REQUEST
    timestamp: float = field(default_factory=time.time)
    duration: float = 0.0
    status_code: Optional[int] = None
    error_message: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    labels: Dict[str, str] = field(default_factory=dict)
    bytes_sent: int = 0
    bytes_received: int = 0
    worker_id: Optional[str] = None
    iteration: int = 0

    @property
    def latency_ms(self) -> float:
        """延迟（毫秒）"""
        return self.latency * 1000.0

    @property
    def is_success(self) -> bool:
        """是否成功"""
        return self.status == SampleStatus.SUCCESS

    @property
    def is_error(self) -> bool:
        """是否错误（包括失败、错误、超时、断言失败）"""
        return self.status != SampleStatus.SUCCESS

    @staticmethod
    def from_step_result(step_result, worker_id: Optional[str] = None) -> "Sample":
        """从 ScenarioStepResult 创建 Sample"""
        latency = step_result.latency
        status = SampleStatus.SUCCESS

        if step_result.error:
            if "Timeout" in step_result.error or "timeout" in step_result.error:
                status = SampleStatus.TIMEOUT
            else:
                status = SampleStatus.ERROR
            error_msg = step_result.error
        elif not step_result.is_success:
            status = SampleStatus.ASSERTION_FAILED
            failed_assertions = [
                a.name for a in step_result.assertion_results if not a.passed
            ]
            error_msg = f"Assertions failed: {', '.join(failed_assertions)}" if failed_assertions else "Unknown assertion failure"
        else:
            error_msg = None

        status_code = step_result.status_code
        tags = [step_result.request_name]
        if step_result.step_name != step_result.request_name:
            tags.append(step_result.step_name)

        labels: Dict[str, str] = {}
        if status_code:
            labels["status_code"] = str(status_code)

        return Sample(
            name=step_result.step_name,
            latency=latency,
            status=status,
            sample_type=SampleType.STEP,
            status_code=status_code,
            error_message=error_msg,
            tags=tags,
            labels=labels,
            worker_id=worker_id,
        )

    @staticmethod
    def from_scenario_result(scenario_result, worker_id: Optional[str] = None) -> "Sample":
        """从 ScenarioResult 创建 Sample"""
        latency = scenario_result.duration
        status = SampleStatus.SUCCESS

        if scenario_result.error:
            status = SampleStatus.ERROR
            error_msg = scenario_result.error
        elif not scenario_result.is_success:
            status = SampleStatus.FAILURE
            error_msg = f"{scenario_result.failed_steps}/{scenario_result.successful_steps + scenario_result.failed_steps} steps failed"
        else:
            error_msg = None

        return Sample(
            name=scenario_result.scenario_name,
            latency=latency,
            status=status,
            sample_type=SampleType.SCENARIO,
            error_message=error_msg,
            tags=[scenario_result.scenario_name],
            labels={"iteration": str(scenario_result.iteration)},
            worker_id=worker_id,
            iteration=scenario_result.iteration,
        )
