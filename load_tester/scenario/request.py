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

    def resolve_template(self, template: str, context: Dict[str, Any]) -> str:
        """解析模板中的变量占位符 ${variable}

        Args:
            template: 模板字符串，如 "https://api.example.com/users/${userId}"
            context: 变量上下文字典

        Returns:
            解析后的字符串
        """
        import re
        pattern = re.compile(r'\$\{([^}]+)\}')

        def replacer(match):
            var_name = match.group(1)
            value = context.get(var_name, match.group(0))
            return str(value)

        return pattern.sub(replacer, template)

    def resolve_headers(self, context: Dict[str, Any]) -> Dict[str, str]:
        """解析头部模板"""
        return {k: self.resolve_template(v, context) for k, v in self.headers.items()}

    def resolve_url(self, context: Dict[str, Any]) -> str:
        """解析URL模板并附加查询参数"""
        base_url = self.resolve_template(self.url, context)
        if self.query_params:
            resolved_params = {
                k: self.resolve_template(str(v), context)
                for k, v in self.query_params.items()
            }
            separator = '&' if '?' in base_url else '?'
            base_url = f"{base_url}{separator}{urlencode(resolved_params)}"
        return base_url

    def resolve_body(self, context: Dict[str, Any]) -> Optional[Union[str, bytes]]:
        """解析请求体"""
        if self.body is None:
            return None

        if isinstance(self.body, (dict, list)):
            body_str = json.dumps(self.body, ensure_ascii=False)
            return self.resolve_template(body_str, context)

        if isinstance(self.body, str):
            return self.resolve_template(self.body, context)

        return self.body

    def get_final_headers(self, context: Dict[str, Any]) -> Dict[str, str]:
        """获取最终请求头，包括自动添加Content-Type"""
        headers = self.resolve_headers(context)
        if self.content_type and 'Content-Type' not in headers:
            headers['Content-Type'] = self.content_type
        elif isinstance(self.body, (dict, list)) and 'Content-Type' not in headers:
            headers['Content-Type'] = 'application/json'
        return headers

    def execute(self, context: Dict[str, Any]) -> "RequestResult":
        """执行HTTP请求的准备工作（实际执行由压力生成器调用HTTP客户端）

        此方法返回请求的配置信息，供压力生成器执行实际HTTP调用。
        """
        url = self.resolve_url(context)
        headers = self.get_final_headers(context)
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
