"""参数化模块

支持多种参数化策略：
- 常量参数：固定值
- 随机参数：整数、浮点数、字符串、选择
- 序列参数：顺序遍历列表，支持循环
- UUID参数：生成唯一ID
- 计数器：递增计数器
- CSV数据源：从CSV文件读取参数
- 日期时间参数：生成时间戳/日期字符串
"""
from __future__ import annotations

import csv
import random
import string
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Union


class ParameterType(str, Enum):
    """参数类型枚举"""
    CONSTANT = "constant"
    RANDOM_INT = "random_int"
    RANDOM_FLOAT = "random_float"
    RANDOM_STRING = "random_string"
    RANDOM_CHOICE = "random_choice"
    SEQUENCE = "sequence"
    UUID = "uuid"
    COUNTER = "counter"
    CSV = "csv"
    DATETIME = "datetime"
    TIMESTAMP = "timestamp"
    CUSTOM = "custom"


class Parameter:
    """参数基类"""

    def __init__(self, name: str, param_type: ParameterType):
        self.name = name
        self.type = param_type

    def next_value(self, context: dict) -> Any:
        """生成下一个参数值，由子类实现"""
        raise NotImplementedError("Subclasses must implement next_value()")

    def reset(self) -> None:
        """重置参数状态（可用于场景迭代）"""
        pass


class ConstantParameter(Parameter):
    """常量参数"""

    def __init__(self, name: str, value: Any):
        super().__init__(name, ParameterType.CONSTANT)
        self.value = value

    def next_value(self, context: dict) -> Any:
        return self.value


class RandomIntParameter(Parameter):
    """随机整数参数"""

    def __init__(self, name: str, min_value: int, max_value: int, seed: Optional[int] = None):
        super().__init__(name, ParameterType.RANDOM_INT)
        self.min_value = min_value
        self.max_value = max_value
        self._rng = random.Random(seed)

    def next_value(self, context: dict) -> Any:
        return self._rng.randint(self.min_value, self.max_value)


class RandomFloatParameter(Parameter):
    """随机浮点数参数"""

    def __init__(
        self,
        name: str,
        min_value: float,
        max_value: float,
        precision: int = 4,
        seed: Optional[int] = None,
    ):
        super().__init__(name, ParameterType.RANDOM_FLOAT)
        self.min_value = min_value
        self.max_value = max_value
        self.precision = precision
        self._rng = random.Random(seed)

    def next_value(self, context: dict) -> Any:
        value = self._rng.uniform(self.min_value, self.max_value)
        return round(value, self.precision)


class RandomStringParameter(Parameter):
    """随机字符串参数"""

    def __init__(
        self,
        name: str,
        min_length: int = 8,
        max_length: int = 16,
        charset: str = string.ascii_letters + string.digits,
        prefix: str = "",
        suffix: str = "",
        seed: Optional[int] = None,
    ):
        super().__init__(name, ParameterType.RANDOM_STRING)
        self.min_length = min_length
        self.max_length = max_length
        self.charset = charset
        self.prefix = prefix
        self.suffix = suffix
        self._rng = random.Random(seed)

    def next_value(self, context: dict) -> Any:
        length = self._rng.randint(self.min_length, self.max_length)
        random_part = "".join(self._rng.choice(self.charset) for _ in range(length))
        return f"{self.prefix}{random_part}{self.suffix}"


class RandomChoiceParameter(Parameter):
    """随机选择参数（从列表中随机选择）"""

    def __init__(
        self,
        name: str,
        choices: List[Any],
        weights: Optional[List[float]] = None,
        seed: Optional[int] = None,
    ):
        super().__init__(name, ParameterType.RANDOM_CHOICE)
        self.choices = choices
        self.weights = weights
        self._rng = random.Random(seed)

    def next_value(self, context: dict) -> Any:
        if self.weights:
            return self._rng.choices(self.choices, weights=self.weights, k=1)[0]
        return self._rng.choice(self.choices)


class SequenceParameter(Parameter):
    """序列参数（顺序遍历列表，支持循环）"""

    def __init__(
        self,
        name: str,
        values: List[Any],
        loop: bool = True,
        per_worker: bool = True,
    ):
        super().__init__(name, ParameterType.SEQUENCE)
        self.values = values
        self.loop = loop
        self.per_worker = per_worker
        self._index = 0

    def next_value(self, context: dict) -> Any:
        if not self.values:
            return None

        if self._index >= len(self.values):
            if self.loop:
                self._index = 0
            else:
                return self.values[-1]

        value = self.values[self._index]
        self._index += 1
        return value

    def reset(self) -> None:
        self._index = 0


class UuidParameter(Parameter):
    """UUID参数"""

    def __init__(self, name: str, uuid_version: int = 4):
        super().__init__(name, ParameterType.UUID)
        self.uuid_version = uuid_version

    def next_value(self, context: dict) -> Any:
        if self.uuid_version == 1:
            return str(uuid.uuid1())
        elif self.uuid_version == 4:
            return str(uuid.uuid4())
        else:
            return str(uuid.uuid4())


class CounterParameter(Parameter):
    """计数器参数（递增计数）"""

    def __init__(
        self,
        name: str,
        start: int = 0,
        step: int = 1,
        width: Optional[int] = None,
        pad_char: str = "0",
    ):
        super().__init__(name, ParameterType.COUNTER)
        self.start = start
        self.step = step
        self.width = width
        self.pad_char = pad_char
        self._value = start

    def next_value(self, context: dict) -> Any:
        current = self._value
        self._value += self.step
        if self.width:
            return str(current).rjust(self.width, self.pad_char)
        return current

    def reset(self) -> None:
        self._value = self.start


class CsvParameter(Parameter):
    """CSV数据源参数

    从CSV文件读取，支持每行产生一个字典或多个独立参数。
    """

    def __init__(
        self,
        name: str,
        csv_path: Union[str, Path],
        columns: Optional[List[str]] = None,
        loop: bool = True,
        delimiter: str = ",",
    ):
        super().__init__(name, ParameterType.CSV)
        self.csv_path = Path(csv_path)
        self.columns = columns
        self.loop = loop
        self.delimiter = delimiter
        self._rows: List[Dict[str, Any]] = []
        self._index = 0
        self._load_csv()

    def _load_csv(self) -> None:
        with open(self.csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=self.delimiter)
            if self.columns:
                self._rows = [{c: row[c] for c in self.columns if c in row} for row in reader]
            else:
                self._rows = [dict(row) for row in reader]

    def next_value(self, context: dict) -> Any:
        if not self._rows:
            return {}

        if self._index >= len(self._rows):
            if self.loop:
                self._index = 0
            else:
                return self._rows[-1]

        value = self._rows[self._index]
        self._index += 1
        return value

    def reset(self) -> None:
        self._index = 0


class DatetimeParameter(Parameter):
    """日期时间参数"""

    def __init__(
        self,
        name: str,
        format_str: str = "%Y-%m-%dT%H:%M:%S",
        offset_hours: float = 0,
        utc: bool = False,
    ):
        super().__init__(name, ParameterType.DATETIME)
        self.format_str = format_str
        self.offset_hours = offset_hours
        self.utc = utc

    def next_value(self, context: dict) -> Any:
        if self.utc:
            now = datetime.utcnow()
        else:
            now = datetime.now()
        if self.offset_hours:
            now = now + timedelta(hours=self.offset_hours)
        return now.strftime(self.format_str)


class TimestampParameter(Parameter):
    """时间戳参数"""

    def __init__(self, name: str, unit: str = "s", offset_seconds: float = 0):
        super().__init__(name, ParameterType.TIMESTAMP)
        self.unit = unit
        self.offset_seconds = offset_seconds

    def next_value(self, context: dict) -> Any:
        ts = time.time() + self.offset_seconds
        if self.unit == "ms":
            return int(ts * 1000)
        elif self.unit == "us":
            return int(ts * 1_000_000)
        else:
            return int(ts)


class CustomParameter(Parameter):
    """自定义参数

    支持传入任意生成函数，接收context参数返回值。
    """

    def __init__(
        self,
        name: str,
        generator: Callable[[dict], Any],
        reset_func: Optional[Callable[[], None]] = None,
    ):
        super().__init__(name, ParameterType.CUSTOM)
        self.generator = generator
        self.reset_func = reset_func

    def next_value(self, context: dict) -> Any:
        return self.generator(context)

    def reset(self) -> None:
        if self.reset_func:
            self.reset_func()


@dataclass
class ParameterSet:
    """参数集合

    管理一组参数，提供统一的获取接口，支持参数依赖解析。
    """
    parameters: List[Parameter] = field(default_factory=list)
    _param_map: Dict[str, Parameter] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._rebuild_map()

    def _rebuild_map(self) -> None:
        self._param_map = {p.name: p for p in self.parameters}

    def add(self, param: Parameter) -> "ParameterSet":
        """添加参数"""
        self.parameters.append(param)
        self._param_map[param.name] = param
        return self

    def get(self, name: str) -> Optional[Parameter]:
        """按名称获取参数"""
        return self._param_map.get(name)

    def generate(self, context: Optional[dict] = None) -> dict:
        """生成所有参数值

        Args:
            context: 已有上下文，参数生成器可引用

        Returns:
            参数名字到值的映射字典
        """
        if context is None:
            context = {}

        result = dict(context)
        for param in self.parameters:
            try:
                result[param.name] = param.next_value(result)
            except Exception as e:
                result[param.name] = None
                result[f"__error_{param.name}"] = str(e)
        return result

    def reset(self) -> None:
        """重置所有参数状态"""
        for param in self.parameters:
            param.reset()

    def __iter__(self) -> Iterator[Parameter]:
        return iter(self.parameters)

    def __len__(self) -> int:
        return len(self.parameters)
