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


class CsvReadMode(str, Enum):
    """CSV读取模式"""
    SEQUENTIAL = "sequential"      # 顺序读取（默认）
    RANDOM = "random"              # 随机读取
    WORKER_SHARDED = "worker_sharded"  # 按Worker分片（每个Worker读取不同行）


class Parameter:
    """参数基类"""

    _auto_name_counter = 0

    def __init__(self, name: Optional[str] = None, param_type: Optional[ParameterType] = None):
        if name is None:
            Parameter._auto_name_counter += 1
            name = f"inline_param_{Parameter._auto_name_counter}"
        self.name = name
        self.type = param_type
        # 统计
        self._call_count = 0  # next_value 调用次数

    def next_value(self, context: dict) -> Any:
        """生成下一个参数值，由子类实现"""
        raise NotImplementedError("Subclasses must implement next_value()")

    def reset(self) -> None:
        """重置参数状态（可用于场景迭代）"""
        pass

    def get_stats(self) -> dict:
        """获取参数使用统计

        Returns:
            包含调用次数、循环次数等信息的字典
        """
        return {
            "name": self.name,
            "type": self.type.value if self.type else "unknown",
            "call_count": self._call_count,
        }

    def clone(self) -> "Parameter":
        """克隆一个参数实例（每个Worker独立一份，避免并发问题）

        子类应重写此方法以正确复制状态。
        """
        import copy
        return copy.deepcopy(self)


class ConstantParameter(Parameter):
    """常量参数"""

    def __init__(self, *args, name: Optional[str] = None, value: Any = None, **kwargs):
        super().__init__(name, ParameterType.CONSTANT)
        # 兼容两种调用方式: ConstantParameter("name", value) 或 ConstantParameter(value="val", name="n")
        if len(args) == 2 and value is None:
            self.name = args[0] if self.name.startswith("inline_param_") else self.name
            self.value = args[1]
        elif len(args) == 1 and value is None:
            # 只有一个位置参数，假设是 value，name 自动生成或从 keyword 来
            self.value = args[0]
        else:
            self.value = value

    def next_value(self, context: dict) -> Any:
        return self.value


class RandomIntParameter(Parameter):
    """随机整数参数"""

    def __init__(self, *args, name: Optional[str] = None, min_value: Optional[int] = None, max_value: Optional[int] = None, seed: Optional[int] = None, **kwargs):
        super().__init__(name, ParameterType.RANDOM_INT)
        # 兼容: RandomIntParameter("name", 1, 100) 或 RandomIntParameter(min_value=1, max_value=100)
        if len(args) >= 3:
            self.name = args[0] if self.name.startswith("inline_param_") else self.name
            self.min_value = args[1]
            self.max_value = args[2]
        elif len(args) == 2:
            # 两个位置参数，假设是 min, max
            self.min_value = args[0]
            self.max_value = args[1]
        else:
            self.min_value = min_value
            self.max_value = max_value
        self._rng = random.Random(seed)

    def next_value(self, context: dict) -> Any:
        return self._rng.randint(self.min_value, self.max_value)


class RandomFloatParameter(Parameter):
    """随机浮点数参数"""

    def __init__(
        self,
        *args,
        name: Optional[str] = None,
        min_value: Optional[float] = None,
        max_value: Optional[float] = None,
        precision: int = 4,
        seed: Optional[int] = None,
        **kwargs,
    ):
        super().__init__(name, ParameterType.RANDOM_FLOAT)
        if len(args) >= 3:
            self.name = args[0] if self.name.startswith("inline_param_") else self.name
            self.min_value = args[1]
            self.max_value = args[2]
        elif len(args) == 2:
            self.min_value = args[0]
            self.max_value = args[1]
        else:
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
        *args,
        name: Optional[str] = None,
        min_length: int = 8,
        max_length: int = 16,
        charset: str = string.ascii_letters + string.digits,
        prefix: str = "",
        suffix: str = "",
        seed: Optional[int] = None,
        **kwargs,
    ):
        super().__init__(name, ParameterType.RANDOM_STRING)
        if len(args) >= 1 and isinstance(args[0], str) and len(args) > 1:
            self.name = args[0] if self.name.startswith("inline_param_") else self.name
            args = args[1:]
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
        *args,
        name: Optional[str] = None,
        choices: Optional[List[Any]] = None,
        weights: Optional[List[float]] = None,
        seed: Optional[int] = None,
        **kwargs,
    ):
        super().__init__(name, ParameterType.RANDOM_CHOICE)
        if len(args) >= 2:
            self.name = args[0] if self.name.startswith("inline_param_") else self.name
            self.choices = args[1]
        elif len(args) == 1:
            # 一个位置参数，假设是 choices
            self.choices = args[0]
        else:
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
        *args,
        name: Optional[str] = None,
        values: Optional[List[Any]] = None,
        loop: bool = True,
        per_worker: bool = True,
        **kwargs,
    ):
        super().__init__(name, ParameterType.SEQUENCE)
        if len(args) >= 2:
            self.name = args[0] if self.name.startswith("inline_param_") else self.name
            self.values = args[1]
        elif len(args) == 1:
            self.values = args[0]
        else:
            self.values = values or []
        self.loop = loop
        self.per_worker = per_worker
        self._index = 0
        self._loop_count = 0
        self._initial_values = list(self.values) if self.values else []

    def next_value(self, context: dict) -> Any:
        self._call_count += 1
        if not self.values:
            return None

        if self._index >= len(self.values):
            if self.loop:
                self._index = 0
                self._loop_count += 1
            else:
                return self.values[-1]

        value = self.values[self._index]
        self._index += 1
        return value

    def reset(self) -> None:
        self._index = 0
        self._loop_count = 0

    def get_stats(self) -> dict:
        stats = super().get_stats()
        stats.update({
            "total_values": len(self.values),
            "current_index": self._index,
            "loop_count": self._loop_count,
            "looped": self._loop_count > 0,
        })
        return stats

    def clone(self) -> "SequenceParameter":
        p = SequenceParameter(
            name=self.name,
            values=list(self._initial_values),
            loop=self.loop,
            per_worker=self.per_worker,
        )
        return p


class UuidParameter(Parameter):
    """UUID参数"""

    def __init__(self, *args, name: Optional[str] = None, uuid_version: int = 4, **kwargs):
        super().__init__(name, ParameterType.UUID)
        if len(args) >= 1:
            self.name = args[0] if self.name.startswith("inline_param_") else self.name
            if len(args) >= 2:
                self.uuid_version = args[1]
            else:
                self.uuid_version = uuid_version
        else:
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
        *args,
        name: Optional[str] = None,
        start: int = 0,
        step: int = 1,
        width: Optional[int] = None,
        pad_char: str = "0",
        **kwargs,
    ):
        super().__init__(name, ParameterType.COUNTER)
        if len(args) >= 1:
            self.name = args[0] if self.name.startswith("inline_param_") else self.name
            if len(args) >= 2:
                self.start = args[1]
            else:
                self.start = start
        else:
            self.start = start
        self.step = step
        self.width = width
        self.pad_char = pad_char
        self._value = self.start
        self._initial_start = self.start

    def next_value(self, context: dict) -> Any:
        self._call_count += 1
        current = self._value
        self._value += self.step
        if self.width:
            return str(current).rjust(self.width, self.pad_char)
        return current

    def reset(self) -> None:
        self._value = self.start

    def get_stats(self) -> dict:
        stats = super().get_stats()
        stats.update({
            "start": self.start,
            "step": self.step,
            "current_value": self._value,
            "total_increments": self._call_count,
        })
        return stats

    def clone(self) -> "CounterParameter":
        p = CounterParameter(
            name=self.name,
            start=self._initial_start,
            step=self.step,
            width=self.width,
            pad_char=self.pad_char,
        )
        return p


class CsvParameter(Parameter):
    """CSV数据源参数

    从CSV文件读取，支持每行产生一个字典或多个独立参数。
    支持3种读取模式：
    - sequential: 顺序读取（默认）
    - random: 随机读取
    - worker_sharded: 按Worker分片（每个Worker读取不同的行段）
    """

    def __init__(
        self,
        *args,
        name: Optional[str] = None,
        csv_path: Optional[Union[str, Path]] = None,
        columns: Optional[List[str]] = None,
        loop: bool = True,
        delimiter: str = ",",
        mode: Union[CsvReadMode, str] = CsvReadMode.SEQUENTIAL,
        worker_id: Optional[str] = None,
        total_workers: int = 1,
        seed: Optional[int] = None,
        **kwargs,
    ):
        super().__init__(name, ParameterType.CSV)
        if len(args) >= 2:
            self.name = args[0] if self.name.startswith("inline_param_") else self.name
            self.csv_path = Path(args[1])
        elif len(args) == 1:
            self.csv_path = Path(args[0])
        else:
            self.csv_path = Path(csv_path) if csv_path else None
        self.columns = columns
        self.loop = loop
        self.delimiter = delimiter
        self.mode = CsvReadMode(mode) if isinstance(mode, str) else mode
        self._worker_id = worker_id
        self._total_workers = total_workers
        self._seed = seed

        self._rows: List[Dict[str, Any]] = []
        self._index = 0
        self._loop_count = 0
        self._rng = random.Random(seed)
        self._sharded_rows: List[Dict[str, Any]] = []  # worker分片后的行
        self._initial_rows: List[Dict[str, Any]] = []

        if self.csv_path:
            self._load_csv()
            self._init_sharding()

    def _load_csv(self) -> None:
        with open(self.csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=self.delimiter)
            if self.columns:
                self._rows = [{c: row[c] for c in self.columns if c in row} for row in reader]
            else:
                self._rows = [dict(row) for row in reader]
        self._initial_rows = list(self._rows)

    def _init_sharding(self) -> None:
        """根据worker分片初始化数据"""
        if self.mode == CsvReadMode.WORKER_SHARDED and self._rows:
            total = len(self._rows)
            per_worker = max(1, total // self._total_workers)
            # 通过 worker_id 哈希或序号确定分片
            worker_idx = 0
            if self._worker_id:
                # 从 worker id 中提取数字序号
                import re
                match = re.search(r'(\d+)', str(self._worker_id))
                if match:
                    worker_idx = int(match.group(1)) % self._total_workers
                else:
                    worker_idx = hash(self._worker_id) % self._total_workers

            start = worker_idx * per_worker
            # 最后一个worker取剩下的所有行
            if worker_idx == self._total_workers - 1:
                self._sharded_rows = self._rows[start:]
            else:
                self._sharded_rows = self._rows[start:start + per_worker]
        else:
            self._sharded_rows = self._rows

    def set_worker_context(self, worker_id: str, total_workers: int) -> None:
        """设置Worker上下文（用于分片模式）"""
        self._worker_id = worker_id
        self._total_workers = max(1, total_workers)
        self._init_sharding()
        self._index = 0
        self._loop_count = 0

    def next_value(self, context: dict) -> Any:
        self._call_count += 1
        rows = self._sharded_rows if self.mode == CsvReadMode.WORKER_SHARDED else self._rows

        if not rows:
            return {}

        if self.mode == CsvReadMode.RANDOM:
            # 随机模式：每次随机选一行
            idx = self._rng.randint(0, len(rows) - 1)
            return rows[idx]

        # 顺序 / 分片模式
        if self._index >= len(rows):
            if self.loop:
                self._index = 0
                self._loop_count += 1
            else:
                return rows[-1]

        value = rows[self._index]
        self._index += 1
        return value

    def reset(self) -> None:
        self._index = 0
        self._loop_count = 0

    def get_stats(self) -> dict:
        stats = super().get_stats()
        rows = self._sharded_rows if self.mode == CsvReadMode.WORKER_SHARDED else self._rows
        total_rows = len(rows)
        stats.update({
            "csv_path": str(self.csv_path) if self.csv_path else None,
            "total_rows_total": len(self._rows),
            "total_rows_available": total_rows,
            "mode": self.mode.value,
            "loop": self.loop,
            "current_index": self._index,
            "loop_count": self._loop_count,
            "looped": self._loop_count > 0,
            "rows_used": min(self._call_count, total_rows) if self.mode != CsvReadMode.RANDOM else self._call_count,
            "recycled": self._loop_count > 0 or (self.mode == CsvReadMode.RANDOM and self._call_count > total_rows),
        })
        return stats

    def clone(self) -> "CsvParameter":
        p = CsvParameter(
            name=self.name,
            csv_path=str(self.csv_path) if self.csv_path else None,
            columns=list(self.columns) if self.columns else None,
            loop=self.loop,
            delimiter=self.delimiter,
            mode=self.mode,
            worker_id=self._worker_id,
            total_workers=self._total_workers,
            seed=self._seed,
        )
        return p


class DatetimeParameter(Parameter):
    """日期时间参数"""

    def __init__(
        self,
        *args,
        name: Optional[str] = None,
        format_str: str = "%Y-%m-%dT%H:%M:%S",
        offset_hours: float = 0,
        utc: bool = False,
        **kwargs,
    ):
        super().__init__(name, ParameterType.DATETIME)
        if len(args) >= 1:
            self.name = args[0] if self.name.startswith("inline_param_") else self.name
            if len(args) >= 2:
                self.format_str = args[1]
            else:
                self.format_str = format_str
        else:
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

    def __init__(self, *args, name: Optional[str] = None, unit: str = "s", offset_seconds: float = 0, **kwargs):
        super().__init__(name, ParameterType.TIMESTAMP)
        if len(args) >= 1:
            self.name = args[0] if self.name.startswith("inline_param_") else self.name
            if len(args) >= 2:
                self.unit = args[1]
            else:
                self.unit = unit
        else:
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
        *args,
        name: Optional[str] = None,
        generator: Optional[Callable[[dict], Any]] = None,
        reset_func: Optional[Callable[[], None]] = None,
        **kwargs,
    ):
        super().__init__(name, ParameterType.CUSTOM)
        if len(args) >= 2:
            self.name = args[0] if self.name.startswith("inline_param_") else self.name
            self.generator = args[1]
        elif len(args) == 1:
            self.generator = args[0]
        else:
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

    def clone(self) -> "ParameterSet":
        """克隆一个参数集合（每个Worker独立一份，避免并发问题）"""
        cloned = ParameterSet(parameters=[])
        for param in self.parameters:
            cloned.parameters.append(param.clone())
        cloned._rebuild_map()
        return cloned

    def set_worker_context(self, worker_id: str, total_workers: int) -> None:
        """为所有CSV参数设置Worker上下文（用于分片模式）"""
        for param in self.parameters:
            if hasattr(param, 'set_worker_context'):
                param.set_worker_context(worker_id, total_workers)

    def get_stats(self) -> List[dict]:
        """获取所有参数的使用统计"""
        return [p.get_stats() for p in self.parameters]

    def get_csv_stats(self) -> List[dict]:
        """仅获取CSV参数的统计信息（用于报告）"""
        return [
            p.get_stats()
            for p in self.parameters
            if p.type == ParameterType.CSV
        ]

    def __iter__(self) -> Iterator[Parameter]:
        return iter(self.parameters)

    def __len__(self) -> int:
        return len(self.parameters)
