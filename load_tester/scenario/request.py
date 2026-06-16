"""请求定义模块

支持HTTP请求的完整配置，包括方法、URL、头部、查询参数、请求体、超时等。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union
from urllib.parse import urlencode


class HttpMethod(str, Enum):
    """HTTP方法枚举"""
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"


RequestBody = Union[str, bytes, Dict[str, Any], List[Any]]
ParamValue = Union[str, int, float, bool]


@dataclass
class Request:
    """请求基类，定义通用请求属性"""
    name: str
    description: Optional[str] = None
    timeout: float = 30.0
    tags: List[str] = field(default_factory=list)

    def execute(self, context: Dict[str, Any]) -> "RequestResult":
        """执行请求，由子类实现"""
        raise NotImplementedError("Subclasses must implement execute()")


@dataclass
class HttpRequest(Request):
    """HTTP请求定义

    支持完整的HTTP请求配置，URL和头部支持模板变量，
    在执行时从场景上下文中解析。
    """
    method: HttpMethod = HttpMethod.GET
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    query_params: Dict[str, ParamValue] = field(default_factory=dict)
    body: Optional[RequestBody] = None
    content_type: Optional[str] = None
    auth: Optional[tuple[str, str]] = None
    allow_redirects: bool = True
    verify_ssl: bool = True

    def _resolve_value(self, value: Any, context: Dict[str, Any]) -> Any:
        """展开值，如果是 Parameter 对象则调用 next_value() 生成值"""
        from .parameter import Parameter
        if isinstance(value, Parameter):
            try:
                return value.next_value(context)
            except Exception:
                return None
        return value

    def resolve_template(self, template: str, context: Dict[str, Any]) -> str:
        """解析模板中的变量占位符 ${variable}

        支持点号路径访问，如 ${user.product_id} 将从 context['user']['product_id'] 取值。

        Args:
            template: 模板字符串，如 "https://api.example.com/users/${userId}"
            context: 变量上下文字典

        Returns:
            解析后的字符串
        """
        import re
        pattern = re.compile(r'\$\{([^}]+)\}')

        def _resolve_path(path: str, ctx: Dict[str, Any]) -> Any:
            """解析点号路径，如 'user.address.city'"""
            parts = path.split('.')
            current: Any = ctx
            for part in parts:
                if isinstance(current, dict):
                    if part in current:
                        current = current[part]
                    else:
                        return None
                elif hasattr(current, part):
                    current = getattr(current, part)
                else:
                    return None
            return current

        def replacer(match):
            var_path = match.group(1)
            value = _resolve_path(var_path, context)
            if value is None:
                return match.group(0)
            return str(value)

        return pattern.sub(replacer, template)

    def resolve_headers(self, context: Dict[str, Any], extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """解析头部模板，合并额外头（步骤头优先覆盖默认头）"""
        headers = {}
        # 先应用额外头（场景默认头）
        if extra_headers:
            for k, v in extra_headers.items():
                headers[k] = self.resolve_template(v if isinstance(v, str) else str(v), context)
        # 再应用步骤自定义头（覆盖默认头）
        for k, v in self.headers.items():
            resolved_v = self._resolve_value(v, context)
            headers[k] = self.resolve_template(str(resolved_v), context)
        return headers

    def resolve_url(self, context: Dict[str, Any]) -> str:
        """解析URL模板并附加查询参数"""
        base_url = self.resolve_template(self.url, context)
        if self.query_params:
            resolved_params = {}
            for k, v in self.query_params.items():
                resolved_v = self._resolve_value(v, context)
                resolved_params[k] = self.resolve_template(str(resolved_v), context)
            separator = '&' if '?' in base_url else '?'
            base_url = f"{base_url}{separator}{urlencode(resolved_params)}"
        return base_url

    def resolve_body(self, context: Dict[str, Any]) -> Optional[Union[str, bytes]]:
        """解析请求体，支持内联Parameter对象展开"""
        if self.body is None:
            return None

        # 先展开内联 Parameter 对象
        def walk_and_resolve(obj):
            if isinstance(obj, dict):
                return {k: walk_and_resolve(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [walk_and_resolve(v) for v in obj]
            else:
                return self._resolve_value(obj, context)

        resolved_body = walk_and_resolve(self.body)

        if isinstance(resolved_body, (dict, list)):
            body_str = json.dumps(resolved_body, ensure_ascii=False)
            return self.resolve_template(body_str, context)

        if isinstance(resolved_body, str):
            return self.resolve_template(resolved_body, context)

        return resolved_body

    def get_final_headers(self, context: Dict[str, Any], extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """获取最终请求头，包括自动添加Content-Type

        Args:
            context: 变量上下文
            extra_headers: 额外的头部（通常是场景默认头，优先级低于步骤自定义头）
        """
        headers = self.resolve_headers(context, extra_headers)
        if self.content_type and 'Content-Type' not in headers:
            headers['Content-Type'] = self.content_type
        elif isinstance(self.body, (dict, list)) and 'Content-Type' not in headers:
            headers['Content-Type'] = 'application/json'
        return headers

    def execute(self, context: Dict[str, Any], extra_headers: Optional[Dict[str, str]] = None) -> "RequestResult":
        """执行HTTP请求的准备工作（实际执行由压力生成器调用HTTP客户端）

        Args:
            context: 变量上下文
            extra_headers: 额外默认头（场景级别），步骤自定义头会覆盖同名头

        Returns:
            请求的配置信息，供压力生成器执行实际HTTP调用。
        """
        url = self.resolve_url(context)
        headers = self.get_final_headers(context, extra_headers)
        body = self.resolve_body(context)

        return RequestResult(
            request_name=self.name,
            method=self.method.value,
            url=url,
            headers=headers,
            body=body,
            timeout=self.timeout,
            allow_redirects=self.allow_redirects,
            verify_ssl=self.verify_ssl,
            auth=self.auth,
        )


@dataclass
class RequestResult:
    """请求执行的配置结果"""
    request_name: str
    method: str
    url: str
    headers: Dict[str, str]
    body: Optional[Union[str, bytes]]
    timeout: float
    allow_redirects: bool
    verify_ssl: bool
    auth: Optional[tuple[str, str]] = None

    def to_curl(self) -> str:
        """生成curl命令用于调试"""
        parts = [f"curl -X {self.method}"]

        if self.headers:
            for k, v in self.headers.items():
                parts.append(f'-H "{k}: {v}"')

        if self.body:
            if isinstance(self.body, bytes):
                parts.append(f'--data-binary "@<binary_data>"')
            else:
                parts.append(f"-d '{self.body}'")

        parts.append(f'"{self.url}"')
        return " ".join(parts)


@dataclass
class ResponseData:
    """HTTP响应数据"""
    status_code: int
    headers: Dict[str, str]
    body: Optional[str]
    latency: float
    timestamp: float
    error: Optional[str] = None

    def json(self) -> Any:
        """解析JSON响应体"""
        if self.body:
            return json.loads(self.body)
        return None

    def is_success(self) -> bool:
        """判断是否为成功响应（2xx/3xx）"""
        return self.error is None and 200 <= self.status_code < 400
