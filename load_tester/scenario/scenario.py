"""场景编排模块

定义压测场景的执行流：
- ScenarioStep: 单个步骤（请求 + 断言 + 提取器）
- ScenarioContext: 步骤间共享的上下文，用于传递数据
- Scenario: 完整压测场景，包含步骤序列、参数集、执行配置
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

from .assertion import Assertion, all_passed, assert_all
from .parameter import Parameter, ParameterSet
from .request import HttpRequest, RequestResult, ResponseData


@dataclass
class Extractor:
    """从响应中提取数据到上下文

    支持多种提取方式：
    - JSON路径提取
    - 正则表达式提取
    - 响应头提取
    - 自定义提取函数
    """
    name: str
    target: str
    source: str = "json_path"
    default: Any = None

    def extract(self, response: ResponseData, context: dict) -> Any:
        """从响应中提取数据

        Args:
            response: HTTP响应
            context: 当前上下文

        Returns:
            提取的值
        """
        try:
            if self.source == "json_path":
                data = response.json()
                return self._json_path(data, self.target)
            elif self.source == "regex":
                body = response.body or ""
                match = re.search(self.target, body)
                return match.group(1) if match and match.groups() else (match.group(0) if match else self.default)
            elif self.source == "header":
                headers_lower = {k.lower(): v for k, v in response.headers.items()}
                return headers_lower.get(self.target.lower(), self.default)
            elif self.source == "status_code":
                return response.status_code
            elif self.source == "body":
                return response.body or self.default
            else:
                return self.default
        except Exception:
            return self.default

    def _json_path(self, data: Any, path: str) -> Any:
        """简单的JSON路径访问"""
        if not path or data is None:
            return self.default

        parts = path.split(".")
        current = data

        for part in parts:
            array_match = re.match(r'^(\w+)\[(\d+)\]$', part)
            if array_match:
                field_name, idx_str = array_match.groups()
                if isinstance(current, dict) and field_name in current:
                    current = current[field_name]
                    idx = int(idx_str)
                    if isinstance(current, list) and len(current) > idx:
                        current = current[idx]
                    else:
                        return self.default
                else:
                    return self.default
            else:
                if isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    return self.default

        return current


@dataclass
class ScenarioStepResult:
    """单步执行结果"""
    step_name: str
    request_name: str
    started_at: float
    completed_at: float
    latency: float
    response: Optional[ResponseData] = None
    assertion_results: List = field(default_factory=list)
    extracted_values: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def is_success(self) -> bool:
        """步骤是否成功（无错误且所有断言通过）"""
        return self.error is None and all_passed(self.assertion_results)

    @property
    def status_code(self) -> Optional[int]:
        return self.response.status_code if self.response else None


@dataclass
class ScenarioStep:
    """场景中的单个步骤

    包含：请求定义、断言列表、数据提取器、执行配置
    """
    name: str
    request: HttpRequest
    assertions: List[Assertion] = field(default_factory=list)
    extractors: List[Extractor] = field(default_factory=list)
    think_time: float = 0.0
    continue_on_failure: bool = False
    weight: int = 1
    enabled: bool = True
    qps_limit: Optional[float] = None  # 步骤级QPS限制（每秒最大请求数）

    def execute(
        self,
        context: "ScenarioContext",
        http_executor: Callable[[RequestResult], ResponseData],
        default_headers: Optional[Dict[str, str]] = None,
    ) -> ScenarioStepResult:
        """执行单个步骤

        Args:
            context: 场景上下文
            http_executor: HTTP执行函数，接收RequestResult返回ResponseData
            default_headers: 场景级别的默认请求头，步骤自定义头优先覆盖

        Returns:
            步骤执行结果
        """
        started_at = time.perf_counter()
        result = ScenarioStepResult(
            step_name=self.name,
            request_name=self.request.name,
            started_at=started_at,
            completed_at=started_at,
            latency=0,
        )

        try:
            # 1. 准备请求（解析模板变量 + 合并默认头）
            request_result = self.request.execute(context.variables, default_headers)

            # 2. 执行HTTP请求
            response = http_executor(request_result)
            result.response = response

            if response.error:
                result.error = response.error

            # 3. 执行断言
            if self.assertions:
                result.assertion_results = assert_all(response, context.variables, self.assertions)

            # 4. 执行数据提取器并更新上下文
            for extractor in self.extractors:
                value = extractor.extract(response, context.variables)
                result.extracted_values[extractor.name] = value
                context.variables[extractor.name] = value

        except Exception as e:
            result.error = f"Step execution error: {type(e).__name__}: {e}"

        result.completed_at = time.perf_counter()
        result.latency = result.completed_at - result.started_at

        return result


@dataclass
class ScenarioContext:
    """场景执行上下文

    在步骤间共享数据，维护用户会话状态。
    每个虚拟用户拥有独立的上下文实例。
    """
    variables: Dict[str, Any] = field(default_factory=dict)
    cookies: Dict[str, str] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    user_id: str = ""
    iteration: int = 0
    last_error: Optional[str] = None

    def set(self, key: str, value: Any) -> None:
        self.variables[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.variables.get(key, default)

    def update(self, data: Dict[str, Any]) -> None:
        self.variables.update(data)

    def clear(self) -> None:
        self.variables.clear()
        self.cookies.clear()
        self.headers.clear()
        self.last_error = None


@dataclass
class ScenarioResult:
    """场景单次完整执行结果"""
    scenario_name: str
    user_id: str
    iteration: int
    started_at: float
    completed_at: float
    duration: float
    steps: List[ScenarioStepResult] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def is_success(self) -> bool:
        return self.error is None and all(s.is_success for s in self.steps)

    @property
    def total_latency(self) -> float:
        return sum(s.latency for s in self.steps)

    @property
    def successful_steps(self) -> int:
        return sum(1 for s in self.steps if s.is_success)

    @property
    def failed_steps(self) -> int:
        return sum(1 for s in self.steps if not s.is_success)


@dataclass
class Scenario:
    """压测场景定义

    一个场景代表一个完整的用户业务流程，包含多个步骤。
    支持：
    - 步骤序列编排
    - 参数化配置
    - 全局默认设置
    - 前置/后置钩子
    """
    name: str
    steps: List[ScenarioStep] = field(default_factory=list)
    parameters: ParameterSet = field(default_factory=ParameterSet)
    base_url: str = ""
    default_headers: Dict[str, str] = field(default_factory=dict)
    setup_hook: Optional[Callable[[ScenarioContext], None]] = None
    teardown_hook: Optional[Callable[[ScenarioContext], None]] = None
    iteration_pause: float = 0.0
    description: str = ""

    def add_step(self, step: ScenarioStep) -> "Scenario":
        """添加步骤"""
        self.steps.append(step)
        return self

    def add_parameter(self, param: Parameter) -> "Scenario":
        """添加参数"""
        self.parameters.add(param)
        return self

    def set_base_url(self, url: str) -> "Scenario":
        """设置基础URL，并自动更新所有请求URL"""
        self.base_url = url
        return self

    def prepare_request_urls(self) -> None:
        """将基础URL应用到所有相对URL的请求"""
        for step in self.steps:
            req = step.request
            if req.url and not (req.url.startswith("http://") or req.url.startswith("https://")):
                if self.base_url:
                    separator = "" if self.base_url.endswith("/") or req.url.startswith("/") else "/"
                    req.url = f"{self.base_url}{separator}{req.url}"

    def create_context(self, user_id: str = "") -> ScenarioContext:
        """创建新的执行上下文"""
        ctx = ScenarioContext(
            user_id=user_id,
            headers=dict(self.default_headers),
        )
        # 生成初始参数值
        ctx.variables.update(self.parameters.generate())
        return ctx

    def run_iteration(
        self,
        context: ScenarioContext,
        http_executor: Callable[[RequestResult], ResponseData],
        pre_step_callback: Optional[Callable[["ScenarioStep"], None]] = None,
    ) -> ScenarioResult:
        """执行一次场景迭代（所有步骤）

        Args:
            context: 执行上下文（包含参数）
            http_executor: HTTP执行函数
            pre_step_callback: 每个步骤执行前的回调（可用于限速等）

        Returns:
            场景执行结果
        """
        started_at = time.perf_counter()
        result = ScenarioResult(
            scenario_name=self.name,
            user_id=context.user_id,
            iteration=context.iteration,
            started_at=started_at,
            completed_at=started_at,
            duration=0,
        )

        try:
            # Setup钩子
            if self.setup_hook:
                self.setup_hook(context)

            # 执行步骤序列
            for step in self.steps:
                if not step.enabled:
                    continue

                # 步骤前回调（如限速）
                if pre_step_callback:
                    pre_step_callback(step)

                step_result = step.execute(context, http_executor, self.default_headers)
                result.steps.append(step_result)

                # 思考时间
                if step.think_time > 0:
                    time.sleep(step.think_time)

                # 失败中止检查
                if not step_result.is_success and not step.continue_on_failure:
                    break

            # Teardown钩子
            if self.teardown_hook:
                self.teardown_hook(context)

        except Exception as e:
            result.error = f"Scenario error: {type(e).__name__}: {e}"
            context.last_error = result.error

        context.iteration += 1
        result.completed_at = time.perf_counter()
        result.duration = result.completed_at - result.started_at

        return result
