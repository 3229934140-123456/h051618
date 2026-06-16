"""工作池模块

实现基于线程池的并发压力生成模型。
每个 Worker 代表一个虚拟用户 (VU)，独立执行场景迭代。
WorkerPool 管理 Worker 的生命周期、并发数动态调整。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Any, Callable, Dict, List, Optional, Set

from ..scenario.request import RequestResult, ResponseData
from ..scenario.scenario import Scenario, ScenarioContext, ScenarioResult

try:
    import requests

    def _default_http_executor(request_result: RequestResult) -> ResponseData:
        """默认的HTTP执行器，使用 requests 库"""
        import time as _time

        start = _time.perf_counter()
        error_msg = None
        status_code = 0
        resp_headers: Dict[str, str] = {}
        resp_body: Optional[str] = None

        try:
            resp = requests.request(
                method=request_result.method,
                url=request_result.url,
                headers=request_result.headers,
                data=request_result.body if not isinstance(request_result.body, bytes) else request_result.body,
                timeout=request_result.timeout,
                allow_redirects=request_result.allow_redirects,
                verify=request_result.verify_ssl,
                auth=request_result.auth,
            )
            status_code = resp.status_code
            resp_headers = dict(resp.headers)
            resp_body = resp.text

        except requests.exceptions.Timeout as e:
            error_msg = f"Timeout: {e}"
            status_code = 0
        except requests.exceptions.ConnectionError as e:
            error_msg = f"ConnectionError: {e}"
            status_code = 0
        except requests.exceptions.RequestException as e:
            error_msg = f"RequestException: {type(e).__name__}: {e}"
            status_code = 0
        except Exception as e:
            error_msg = f"UnexpectedError: {type(e).__name__}: {e}"
            status_code = 0

        latency = _time.perf_counter() - start
        return ResponseData(
            status_code=status_code,
            headers=resp_headers,
            body=resp_body,
            latency=latency,
            timestamp=_time.time(),
            error=error_msg,
        )

except ImportError:
    def _default_http_executor(request_result: RequestResult) -> ResponseData:
        """requests 库不可用时的占位执行器"""
        start = time.perf_counter()
        time.sleep(0.01)
        latency = time.perf_counter() - start
        return ResponseData(
            status_code=200,
            headers={},
            body="OK",
            latency=latency,
            timestamp=time.time(),
            error=None,
        )


@dataclass
class WorkerResult:
    """Worker 执行完一次场景迭代的结果"""
    worker_id: str
    scenario_result: ScenarioResult
    timestamp: float


class Worker:
    """虚拟用户 (Virtual User)

    每个 Worker 是一个独立的执行线程，拥有自己的 ScenarioContext，
    循环执行场景迭代，直到被停止。

    设计要点：
    - 独立上下文：每个 Worker 有独立的 cookies/headers/variables，
      模拟真实的用户会话隔离
    - 可中断：通过 stop_event 优雅停止，避免硬杀线程
    - 结果回调：通过 result_callback 将结果传递给采集器，
      使用无锁队列或回调函数保证高性能
    - 异常隔离：单个 Worker 的异常不会影响其他 Worker
    """

    def __init__(
        self,
        worker_id: str,
        scenario: Scenario,
        http_executor: Callable[[RequestResult], ResponseData] = _default_http_executor,
        result_callback: Optional[Callable[[WorkerResult], None]] = None,
        rate_limiter=None,
        step_rate_limiters: Optional[Dict[str, Any]] = None,
        global_stop_event: Optional[threading.Event] = None,
        total_workers: int = 1,
    ):
        self.worker_id = worker_id
        self.scenario = scenario
        self.http_executor = http_executor
        self.result_callback = result_callback
        self.rate_limiter = rate_limiter
        self.step_rate_limiters = step_rate_limiters or {}
        self.global_stop_event = global_stop_event or threading.Event()
        self._total_workers = total_workers

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._context: Optional[ScenarioContext] = None
        self._iterations_completed: int = 0
        self._iterations_failed: int = 0
        self._is_running = False
        # 每个Worker独立的参数集合（避免并发问题，计数器/CSV独立推进）
        self._parameters: Optional[ParameterSet] = None

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def iterations_completed(self) -> int:
        return self._iterations_completed

    @property
    def iterations_failed(self) -> int:
        return self._iterations_failed

    def _should_stop(self) -> bool:
        return self._stop_event.is_set() or self.global_stop_event.is_set()

    def start(self) -> None:
        """启动 Worker 线程"""
        if self._is_running:
            return

        self._stop_event.clear()
        self._is_running = True
        self._context = self.scenario.create_context(user_id=self.worker_id)
        self._thread = threading.Thread(target=self._run, name=f"Worker-{self.worker_id}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> bool:
        """优雅停止 Worker

        Args:
            timeout: 最长等待时间

        Returns:
            是否在超时前停止成功
        """
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            stopped = not self._thread.is_alive()
        else:
            stopped = True
        self._is_running = False
        return stopped

    def _run(self) -> None:
        """Worker 主循环"""
        try:
            # 初始化独立的参数集合（每个Worker一份，避免并发问题）
            self._parameters = self.scenario.parameters.clone()
            # 设置CSV分片上下文
            self._parameters.set_worker_context(self.worker_id, self._total_workers)

            # 创建上下文（使用独立参数集初始化）
            if self._context is None:
                initial_vars = self._parameters.generate()
                self._context = self.scenario.create_context(
                    user_id=self.worker_id,
                    initial_vars=initial_vars,
                )

            # 包装 http_executor，在每个HTTP请求前获取令牌（按请求限速）
            original_executor = self.http_executor
            rate_limiter = self.rate_limiter

            def rate_limited_executor(request_result):
                if rate_limiter is not None and not self._should_stop():
                    try:
                        rate_limiter.acquire(1)
                    except Exception:
                        pass
                if self._should_stop():
                    # 已停止，返回空响应
                    import time as _t
                    from ..scenario.request import ResponseData
                    return ResponseData(
                        status_code=0,
                        headers={},
                        body=None,
                        latency=0,
                        timestamp=_t.time(),
                        error="Load test stopped",
                    )
                return original_executor(request_result)

            effective_executor = rate_limited_executor

            # 步骤级限速回调
            step_rate_limiters = self.step_rate_limiters
            def pre_step_hook(step):
                if self._should_stop():
                    return
                rl = step_rate_limiters.get(step.name)
                if rl is not None:
                    try:
                        rl.acquire(1)
                    except Exception:
                        pass

            while not self._should_stop():
                # 每次迭代重新生成参数值（不reset，计数器/CSV自然推进）
                if self._parameters is not None:
                    new_params = self._parameters.generate()
                    # 保留已提取的变量（如token、user_id等），但参数化的值重新生成
                    for k, v in new_params.items():
                        self._context.variables[k] = v

                if self._should_stop():
                    break

                # 执行一次场景迭代（限速：全局在http_executor中按请求处理，步骤级在pre_step_hook中）
                try:
                    scenario_result = self.scenario.run_iteration(
                        context=self._context,
                        http_executor=effective_executor,
                        pre_step_callback=pre_step_hook,
                    )

                    self._iterations_completed += 1
                    if not scenario_result.is_success:
                        self._iterations_failed += 1

                    # 回调结果
                    if self.result_callback:
                        worker_result = WorkerResult(
                            worker_id=self.worker_id,
                            scenario_result=scenario_result,
                            timestamp=time.time(),
                        )
                        try:
                            self.result_callback(worker_result)
                        except Exception:
                            pass

                except Exception as e:
                    self._iterations_failed += 1
                    if self.result_callback:
                        from ..scenario.scenario import ScenarioResult as SR
                        now = time.perf_counter()
                        dummy = SR(
                            scenario_name=self.scenario.name,
                            user_id=self.worker_id,
                            iteration=self._context.iteration if self._context else 0,
                            started_at=now,
                            completed_at=now,
                            duration=0,
                            error=f"Worker fatal error: {type(e).__name__}: {e}",
                        )
                        try:
                            self.result_callback(WorkerResult(
                                worker_id=self.worker_id,
                                scenario_result=dummy,
                                timestamp=time.time(),
                            ))
                        except Exception:
                            pass

                # 场景迭代间的暂停
                if self.scenario.iteration_pause > 0 and not self._should_stop():
                    remaining = self.scenario.iteration_pause
                    chunk = 0.01
                    while remaining > 0 and not self._should_stop():
                        sleep_for = min(chunk, remaining)
                        time.sleep(sleep_for)
                        remaining -= sleep_for

        except Exception as e:
            self._is_running = False
            return

        self._is_running = False


class WorkerPool:
    """工作线程池

    管理多个 Worker 的生命周期，支持：
    - 动态调整并发数 (scale_up / scale_down)
    - 优雅启动/停止
    - 统计汇总
    - Worker 异常监控与自动重启

    并发模型设计：
    - 固定并发模型：启动 N 个 Worker，持续运行
    - 阶梯并发：定期调整 Worker 数量
    - 每个 Worker 代表 1 个并发用户 (VU)
    """

    def __init__(
        self,
        scenario: Scenario,
        num_workers: int = 10,
        http_executor: Optional[Callable[[RequestResult], ResponseData]] = None,
        result_callback: Optional[Callable[[WorkerResult], None]] = None,
        rate_limiter=None,
        auto_restart: bool = True,
        global_qps: Optional[float] = None,
    ):
        self.scenario = scenario
        self._initial_workers = num_workers
        self._http_executor = http_executor or _default_http_executor
        self._result_callback = result_callback
        self._rate_limiter = rate_limiter
        self._auto_restart = auto_restart
        self._target_total_workers = num_workers  # 目标总worker数（用于CSV分片）
        self._global_qps = global_qps  # 全局目标QPS（用于权重分摊）

        # 步骤级rate limiter（按步骤名 -> RateLimiter，所有Worker共享）
        self._step_rate_limiters: Dict[str, Any] = {}
        self._init_step_rate_limiters()

        self._workers: Dict[str, Worker] = {}
        self._lock = threading.Lock()
        self._global_stop_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._is_running = False

    def _init_step_rate_limiters(self) -> None:
        """为每个配置了qps_limit的步骤创建独立的令牌桶限速器

        限速优先级（从高到低）：
        1. 步骤本身配置了 qps_limit（硬限制）
        2. 未配置 qps_limit 但配置了 weight 且有 global_qps 时：按权重分摊全局QPS

        注意：全局 QPS 口径是所有 HTTP 请求总和（即真实发出的请求数），
        所以每轮 N 步的场景会贡献 N 个请求，权重模式下服务端看到的总速率 = sum(step_qps) ≈ 配置值
        """
        from .rate_limiter import TokenBucketRateLimiter

        enabled_steps = [s for s in self.scenario.steps if s.enabled]
        if not enabled_steps:
            return

        # 阶段1: 应用步骤自身 qps_limit 的硬限制
        for step in enabled_steps:
            if step.qps_limit is not None and step.qps_limit > 0:
                self._step_rate_limiters[step.name] = TokenBucketRateLimiter(
                    rate_per_second=step.qps_limit,
                )

        # 阶段2: 权重分摊全局QPS（仅对未设置硬限制的步骤生效）
        if self._global_qps and self._global_qps > 0:
            # 计算硬限制已占用的 QPS
            hard_limited_qps = 0.0
            weight_sum = 0
            weighted_steps = []
            for step in enabled_steps:
                if step.name in self._step_rate_limiters:
                    hard_limited_qps += self._step_rate_limiters[step.name].rate_per_second
                else:
                    sw = getattr(step, 'weight', 1) or 1
                    weight_sum += max(0.01, sw)
                    weighted_steps.append(step)

            remaining_qps = max(0.0, self._global_qps - hard_limited_qps)

            # 若还有QPS余量，按权重分摊
            if remaining_qps > 0 and weighted_steps:
                for step in weighted_steps:
                    sw = getattr(step, 'weight', 1) or 1
                    w = max(0.01, sw)
                    step_share = remaining_qps * (w / weight_sum)
                    # 给未设置 qps_limit 的步骤创建权重式限速器

                    existing = self._step_rate_limiters.get(step.name)
                    if existing is None:
                        self._step_rate_limiters[step.name] = TokenBucketRateLimiter(
                            rate_per_second=step_share,
                        )
                    else:
                        pass  # 已存在硬限制，保持不变

        # 保存配置摘要
        self._step_rate_summary = {
            name: r.rate_per_second
            for name, r in self._step_rate_limiters.items()
        }

    @property
    def active_workers(self) -> int:
        """当前活跃 Worker 数"""
        with self._lock:
            return sum(1 for w in self._workers.values() if w.is_running)

    @property
    def total_workers(self) -> int:
        """Worker 总数"""
        with self._lock:
            return len(self._workers)

    @property
    def total_iterations(self) -> int:
        """总迭代次数"""
        with self._lock:
            return sum(w.iterations_completed for w in self._workers.values())

    @property
    def total_failures(self) -> int:
        """总失败次数"""
        with self._lock:
            return sum(w.iterations_failed for w in self._workers.values())

    def _create_worker(self, worker_id: Optional[str] = None) -> Worker:
        """创建新 Worker"""
        wid = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        return Worker(
            worker_id=wid,
            scenario=self.scenario,
            http_executor=self._http_executor,
            result_callback=self._result_callback,
            rate_limiter=self._rate_limiter,
            step_rate_limiters=self._step_rate_limiters,
            global_stop_event=self._global_stop_event,
            total_workers=self._target_total_workers,
        )

    def get_parameter_stats(self) -> List[dict]:
        """收集所有Worker的参数使用统计

        Returns:
            每个Worker的参数统计列表
        """
        all_stats = []
        with self._lock:
            for wid, worker in self._workers.items():
                if hasattr(worker, '_parameters') and worker._parameters is not None:
                    stats = worker._parameters.get_stats()
                    for s in stats:
                        s['worker_id'] = wid
                    all_stats.extend(stats)
        return all_stats

    def get_csv_stats_summary(self) -> List[dict]:
        """获取CSV参数统计的汇总（合并所有Worker）

        Returns:
            每个CSV参数的汇总统计
        """
        csv_by_name: Dict[str, dict] = {}
        with self._lock:
            for wid, worker in self._workers.items():
                if hasattr(worker, '_parameters') and worker._parameters is not None:
                    for csv_stat in worker._parameters.get_csv_stats():
                        name = csv_stat.get('name', 'unknown')
                        call_count = csv_stat.get('call_count', 0)
                        loop_count = csv_stat.get('loop_count', 0)
                        looped = csv_stat.get('looped', False)
                        rows_used = csv_stat.get('rows_used', 0)
                        rows_available = csv_stat.get('total_rows_available', csv_stat.get('total_rows_total', 0))
                        recycled = csv_stat.get('recycled', False)
                        rows_total = csv_stat.get('total_rows_total', 0)

                        if name not in csv_by_name:
                            csv_by_name[name] = {
                                'name': name,
                                'type': 'csv',
                                'csv_path': csv_stat.get('csv_path'),
                                'read_mode': csv_stat.get('mode'),
                                'mode': csv_stat.get('mode'),
                                'total_rows': rows_total,
                                'total_rows_available_sum': 0,
                                'rows_used_max': 0,
                                'rows_used_sum': 0,
                                'total_call_count': 0,
                                'workers_using': 0,
                                'any_looped': False,
                                'any_recycled': False,
                                'loop_counts': [],
                                'rows_used_per_worker': {},
                                'call_count_per_worker': {},
                                'loop_count_per_worker': {},
                            }
                        s = csv_by_name[name]
                        s['total_call_count'] += call_count
                        s['workers_using'] += 1
                        s['total_rows_available_sum'] += rows_available
                        s['rows_used_sum'] += rows_used
                        if rows_used > s['rows_used_max']:
                            s['rows_used_max'] = rows_used
                        if looped:
                            s['any_looped'] = True
                        if recycled:
                            s['any_recycled'] = True
                        s['loop_counts'].append(loop_count)
                        s['rows_used_per_worker'][wid] = rows_used
                        s['call_count_per_worker'][wid] = call_count
                        s['loop_count_per_worker'][wid] = loop_count
                        # 冗余字段，便于报告直接用
                        s['rows_used'] = s['rows_used_max']
                        s['call_count'] = s['total_call_count']
                        s['looped'] = s['any_looped']
                        s['loop_count'] = max(s['loop_counts']) if s['loop_counts'] else 0
                        s['recycled'] = s['any_recycled']
                        s['coverage_pct'] = round(
                            (s['rows_used_max'] / max(1, rows_total)) * 100, 2
                        ) if rows_total else 0
        return list(csv_by_name.values())

    def start(self) -> None:
        """启动所有初始 Worker 并开始监控"""
        if self._is_running:
            return

        self._global_stop_event.clear()
        self._is_running = True

        # 准备场景URL
        self.scenario.prepare_request_urls()

        # 启动初始 Worker
        with self._lock:
            for _ in range(self._initial_workers):
                worker = self._create_worker()
                self._workers[worker.worker_id] = worker
                worker.start()

        # 启动监控线程
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="WorkerPool-Monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def stop(self, timeout_per_worker: float = 5.0) -> None:
        """停止所有 Worker"""
        self._global_stop_event.set()
        self._is_running = False

        with self._lock:
            workers_copy = list(self._workers.values())

        for worker in workers_copy:
            worker.stop(timeout=timeout_per_worker)

    def scale_up(self, count: int) -> int:
        """增加 Worker

        Args:
            count: 增加的数量

        Returns:
            实际新增的 Worker 数量
        """
        if count <= 0:
            return 0

        added = 0
        with self._lock:
            for _ in range(count):
                worker = self._create_worker()
                self._workers[worker.worker_id] = worker
                worker.start()
                added += 1
        return added

    def scale_down(self, count: int) -> int:
        """减少 Worker

        Args:
            count: 减少的数量

        Returns:
            实际停止的 Worker 数量
        """
        if count <= 0:
            return 0

        removed = 0
        with self._lock:
            running_ids = [wid for wid, w in self._workers.items() if w.is_running]
            to_remove = running_ids[:count]
            for wid in to_remove:
                worker = self._workers.pop(wid, None)
                if worker:
                    worker.stop(timeout=2.0)
                    removed += 1
        return removed

    def scale_to(self, target_count: int) -> int:
        """调整到目标并发数

        Returns:
            净变化数量（正=增加，负=减少）
        """
        current = self.active_workers
        diff = target_count - current
        if diff > 0:
            self.scale_up(diff)
        elif diff < 0:
            self.scale_down(-diff)
        return diff

    def _monitor_loop(self) -> None:
        """监控线程：检测死掉的 Worker 并自动重启"""
        while self._is_running:
            if self._auto_restart:
                with self._lock:
                    for wid, worker in list(self._workers.items()):
                        if not worker.is_running and not self._global_stop_event.is_set():
                            # Worker 异常退出，重启
                            self._workers.pop(wid, None)
                            new_worker = self._create_worker()
                            self._workers[new_worker.worker_id] = new_worker
                            new_worker.start()

            # 每500ms检查一次
            for _ in range(5):
                if not self._is_running:
                    break
                time.sleep(0.1)
