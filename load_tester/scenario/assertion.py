"""断言模块

支持对HTTP响应的多种断言：状态码、响应体包含/匹配、JSON路径、
响应头、响应时间阈值等。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, List, Optional, Union

from .request import ResponseData


class AssertionType(str, Enum):
    """断言类型枚举"""
    STATUS_CODE = "status_code"
    STATUS_CODE_IN = "status_code_in"
    STATUS_CODE_NOT = "status_code_not"
    BODY_CONTAINS = "body_contains"
    BODY_NOT_CONTAINS = "body_not_contains"
    BODY_MATCHES = "body_matches"
    JSON_PATH = "json_path"
    HEADER_EXISTS = "header_exists"
    HEADER_VALUE = "header_value"
    LATENCY_THRESHOLD = "latency_threshold"
    CUSTOM = "custom"


@dataclass
class AssertionResult:
    """断言结果"""
    name: str
    type: AssertionType
    passed: bool
    message: str
    expected: Optional[Any] = None
    actual: Optional[Any] = None

    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"[{status}] {self.name}: {self.message}"


class Assertion:
    """断言基类"""

    def __init__(self, name: str, assertion_type: AssertionType):
        self.name = name
        self.type = assertion_type

    def check(self, response: ResponseData, context: dict) -> AssertionResult:
        """执行断言检查，由子类实现"""
        raise NotImplementedError("Subclasses must implement check()")


class StatusCodeAssertion(Assertion):
    """状态码断言"""

    def __init__(self, expected_status: int, name: Optional[str] = None):
        super().__init__(name or f"status_code == {expected_status}", AssertionType.STATUS_CODE)
        self.expected_status = expected_status

    def check(self, response: ResponseData, context: dict) -> AssertionResult:
        passed = response.status_code == self.expected_status
        return AssertionResult(
            name=self.name,
            type=self.type,
            passed=passed,
            message=f"期望状态码 {self.expected_status}，实际 {response.status_code}",
            expected=self.expected_status,
            actual=response.status_code,
        )


class StatusCodeInAssertion(Assertion):
    """状态码在指定列表中断言"""

    def __init__(self, expected_codes: List[int], name: Optional[str] = None):
        super().__init__(name or f"status_code in {expected_codes}", AssertionType.STATUS_CODE_IN)
        self.expected_codes = expected_codes

    def check(self, response: ResponseData, context: dict) -> AssertionResult:
        passed = response.status_code in self.expected_codes
        return AssertionResult(
            name=self.name,
            type=self.type,
            passed=passed,
            message=f"期望状态码在 {self.expected_codes} 中，实际 {response.status_code}",
            expected=self.expected_codes,
            actual=response.status_code,
        )


class SuccessAssertion(StatusCodeInAssertion):
    """成功响应断言（2xx）"""

    def __init__(self, name: Optional[str] = None):
        super().__init__(list(range(200, 300)), name or "successful_response (2xx)")


class BodyContainsAssertion(Assertion):
    """响应体包含指定内容断言"""

    def __init__(self, expected_text: str, case_sensitive: bool = True, name: Optional[str] = None):
        super().__init__(name or f"body contains '{expected_text}'", AssertionType.BODY_CONTAINS)
        self.expected_text = expected_text
        self.case_sensitive = case_sensitive

    def check(self, response: ResponseData, context: dict) -> AssertionResult:
        body = response.body or ""
        expected = self.expected_text
        if not self.case_sensitive:
            body = body.lower()
            expected = expected.lower()
        passed = expected in body
        preview = body[:100] + "..." if len(body) > 100 else body
        return AssertionResult(
            name=self.name,
            type=self.type,
            passed=passed,
            message=f"响应体{'包含' if passed else '不包含'} '{self.expected_text}'，响应体: {preview}",
            expected=self.expected_text,
            actual=preview,
        )


class BodyMatchesAssertion(Assertion):
    """响应体正则匹配断言"""

    def __init__(self, pattern: str, flags: int = 0, name: Optional[str] = None):
        super().__init__(name or f"body matches /{pattern}/", AssertionType.BODY_MATCHES)
        self.pattern = re.compile(pattern, flags)

    def check(self, response: ResponseData, context: dict) -> AssertionResult:
        body = response.body or ""
        match = self.pattern.search(body)
        passed = match is not None
        return AssertionResult(
            name=self.name,
            type=self.type,
            passed=passed,
            message=f"响应体{'匹配' if passed else '不匹配'}正则 {self.pattern.pattern}",
            expected=self.pattern.pattern,
            actual=body[:200] if not passed else match.group(0),
        )


class JsonPathAssertion(Assertion):
    """JSON路径断言

    使用类似JMESPath的简单路径语法:
    - "data.user.id" - 访问嵌套字段
    - "items[0].name" - 访问数组元素
    - "users[].id" - 收集数组中所有元素的字段
    """

    def __init__(
        self,
        json_path: str,
        expected_value: Any = None,
        validator: Optional[Callable[[Any], bool]] = None,
        name: Optional[str] = None,
    ):
        super().__init__(
            name or f"json path '{json_path}'",
            AssertionType.JSON_PATH,
        )
        self.json_path = json_path
        self.expected_value = expected_value
        self.validator = validator

    def _get_value_by_path(self, data: Any, path: str) -> Any:
        """根据路径获取JSON值"""
        if not path:
            return data

        parts = path.split(".")
        current = data

        for part in parts:
            # 处理数组访问: items[0] 或 items[]
            array_match = re.match(r'^(\w+)\[(\d*)\]$', part)
            if array_match:
                field_name = array_match.group(1)
                index_str = array_match.group(2)

                if isinstance(current, dict) and field_name in current:
                    current = current[field_name]
                else:
                    return None

                if index_str == "":
                    # items[] - 收集所有元素
                    return current if isinstance(current, list) else [current]
                else:
                    # items[0] - 访问指定索引
                    index = int(index_str)
                    if isinstance(current, list) and len(current) > index:
                        current = current[index]
                    else:
                        return None
            else:
                # 普通字段访问
                if isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    return None

        return current

    def check(self, response: ResponseData, context: dict) -> AssertionResult:
        try:
            data = response.json()
        except (json.JSONDecodeError, TypeError):
            return AssertionResult(
                name=self.name,
                type=self.type,
                passed=False,
                message="响应体不是有效的JSON",
                expected="valid JSON",
                actual=response.body[:100] if response.body else None,
            )

        actual_value = self._get_value_by_path(data, self.json_path)

        if self.validator:
            try:
                passed = self.validator(actual_value)
                message = f"路径 '{self.json_path}' 自定义验证{'通过' if passed else '失败'}，值={actual_value}"
            except Exception as e:
                passed = False
                message = f"自定义验证器执行异常: {e}"
        else:
            passed = actual_value == self.expected_value
            message = (
                f"路径 '{self.json_path}' 期望值 {self.expected_value}，"
                f"实际值 {actual_value}"
            )

        return AssertionResult(
            name=self.name,
            type=self.type,
            passed=passed,
            message=message,
            expected=self.expected_value,
            actual=actual_value,
        )


class HeaderExistsAssertion(Assertion):
    """响应头存在断言"""

    def __init__(self, header_name: str, name: Optional[str] = None):
        super().__init__(name or f"header '{header_name}' exists", AssertionType.HEADER_EXISTS)
        self.header_name = header_name

    def check(self, response: ResponseData, context: dict) -> AssertionResult:
        headers_lower = {k.lower(): v for k, v in response.headers.items()}
        passed = self.header_name.lower() in headers_lower
        return AssertionResult(
            name=self.name,
            type=self.type,
            passed=passed,
            message=f"响应头{'包含' if passed else '不包含'} '{self.header_name}'",
            expected=f"{self.header_name} exists",
            actual=list(response.headers.keys()),
        )


class HeaderValueAssertion(Assertion):
    """响应头值断言"""

    def __init__(
        self,
        header_name: str,
        expected_value: str,
        exact_match: bool = True,
        name: Optional[str] = None,
    ):
        super().__init__(
            name or f"header '{header_name}' == '{expected_value}'",
            AssertionType.HEADER_VALUE,
        )
        self.header_name = header_name
        self.expected_value = expected_value
        self.exact_match = exact_match

    def check(self, response: ResponseData, context: dict) -> AssertionResult:
        headers_lower = {k.lower(): v for k, v in response.headers.items()}
        actual_value = headers_lower.get(self.header_name.lower())

        if actual_value is None:
            return AssertionResult(
                name=self.name,
                type=self.type,
                passed=False,
                message=f"响应头 '{self.header_name}' 不存在",
                expected=self.expected_value,
                actual=None,
            )

        if self.exact_match:
            passed = actual_value == self.expected_value
        else:
            passed = self.expected_value in actual_value

        return AssertionResult(
            name=self.name,
            type=self.type,
            passed=passed,
            message=(
                f"响应头 '{self.header_name}' 期望值 '{self.expected_value}'，"
                f"实际值 '{actual_value}'"
            ),
            expected=self.expected_value,
            actual=actual_value,
        )


class LatencyThresholdAssertion(Assertion):
    """响应时间阈值断言"""

    def __init__(self, max_latency_ms: float, name: Optional[str] = None):
        super().__init__(
            name or f"latency <= {max_latency_ms}ms",
            AssertionType.LATENCY_THRESHOLD,
        )
        self.max_latency_ms = max_latency_ms

    def check(self, response: ResponseData, context: dict) -> AssertionResult:
        actual_latency_ms = response.latency * 1000
        passed = actual_latency_ms <= self.max_latency_ms
        return AssertionResult(
            name=self.name,
            type=self.type,
            passed=passed,
            message=(
                f"响应延迟阈值 {self.max_latency_ms}ms，"
                f"实际 {actual_latency_ms:.2f}ms"
            ),
            expected=self.max_latency_ms,
            actual=actual_latency_ms,
        )


class CustomAssertion(Assertion):
    """自定义断言

    支持传入任意检查函数，接收response和context参数，返回bool或抛出异常。
    """

    def __init__(
        self,
        check_func: Callable[[ResponseData, dict], bool],
        name: Optional[str] = None,
    ):
        super().__init__(name or "custom_assertion", AssertionType.CUSTOM)
        self.check_func = check_func

    def check(self, response: ResponseData, context: dict) -> AssertionResult:
        try:
            passed = bool(self.check_func(response, context))
            message = "自定义断言通过" if passed else "自定义断言失败"
        except Exception as e:
            passed = False
            message = f"自定义断言执行异常: {e}"

        return AssertionResult(
            name=self.name,
            type=self.type,
            passed=passed,
            message=message,
        )


def assert_all(response: ResponseData, context: dict, assertions: List[Assertion]) -> List[AssertionResult]:
    """批量执行断言并返回结果列表"""
    return [a.check(response, context) for a in assertions]


def all_passed(results: List[AssertionResult]) -> bool:
    """判断所有断言是否全部通过"""
    return all(r.passed for r in results)
