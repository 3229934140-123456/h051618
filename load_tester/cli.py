"""命令行接口 (CLI)

提供命令行入口，支持从Python脚本文件加载场景定义并执行压测。

使用方式：
  python -m load_tester.cli run scenario_script.py --duration 120 --concurrency 50
  python -m load_tester.cli run scenario_script.py --mode step --steps "..."
  python -m load_tester.cli list scenario_script.py
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .engine import LoadTestConfig, LoadTestEngine, LoadTestResult


def _parse_steps_arg(steps_str: str):
    """解析步骤参数字符串

    格式: "120,10,100;120,25,250;120,50,500"
    表示3个阶梯：(时长秒, 并发数, QPS)
    """
    result = []
    for step_str in steps_str.split(";"):
        step_str = step_str.strip()
        if not step_str:
            continue
        parts = step_str.split(",")
        if len(parts) < 2:
            raise ValueError(f"Invalid step format: {step_str}. Need duration,concurrency[,qps]")
        duration = float(parts[0].strip())
        concurrency = int(parts[1].strip())
        qps = float(parts[2].strip()) if len(parts) >= 3 and parts[2].strip() else None
        result.append((duration, concurrency, qps))
    return result


def _load_scenario_from_file(script_path: Path):
    """从Python脚本文件中加载 Scenario 对象

    脚本需要:
    - 定义一个名为 'scenario' 的 Scenario 实例，或
    - 定义 build_scenario() 函数返回 Scenario
    """
    if not script_path.exists():
        raise FileNotFoundError(f"Scenario script not found: {script_path}")

    spec = importlib.util.spec_from_file_location("loadtest_scenario", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load script: {script_path}")

    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(script_path.parent))
    spec.loader.exec_module(module)

    # 优先查找 build_scenario 函数
    if hasattr(module, "build_scenario") and callable(module.build_scenario):
        scenario = module.build_scenario()
        if scenario is None:
            raise ValueError("build_scenario() returned None")
        return scenario

    # 然后查找 scenario 变量
    if hasattr(module, "scenario"):
        return module.scenario

    # 查找第一个 Scenario 类型的变量
    from .scenario import Scenario as ScenarioType
    for name, value in vars(module).items():
        if isinstance(value, ScenarioType) and not name.startswith("_"):
            return value

    raise AttributeError(
        f"Script {script_path} must define 'scenario' variable or 'build_scenario()' function"
    )


def _list_scenario(scenario) -> None:
    """列出场景中的步骤和参数"""
    print(f"\n📋 场景: {scenario.name}")
    if scenario.description:
        print(f"   描述: {scenario.description}")
    if scenario.base_url:
        print(f"   Base URL: {scenario.base_url}")

    print(f"\n   共 {len(scenario.steps)} 个步骤:")
    for i, step in enumerate(scenario.steps, 1):
        enabled = "✓" if step.enabled else "✗"
        req = step.request
        print(f"   [{enabled}] Step {i}: {step.name}")
        print(f"        {req.method.value} {req.url}")
        if step.assertions:
            print(f"        {len(step.assertions)} 个断言")
            for a in step.assertions[:3]:
                print(f"          - {a.name}")
            if len(step.assertions) > 3:
                print(f"          ... 共 {len(step.assertions)} 个")
        if step.extractors:
            print(f"        {len(step.extractors)} 个数据提取器")
            for ex in step.extractors:
                print(f"          - {ex.name} ({ex.source}: {ex.target})")
        if step.think_time:
            print(f"        Think time: {step.think_time}s")
        if step.weight != 1:
            print(f"        Weight: {step.weight}")

    if scenario.parameters:
        print(f"\n   共 {len(scenario.parameters)} 个参数:")
        for param in scenario.parameters:
            print(f"      - {param.name}: {param.type.value}")

    print()


def _build_config_from_args(args, scenario) -> LoadTestConfig:
    """根据命令行参数构建压测配置"""
    steps_cfg = []
    if args.mode == "step":
        if args.steps:
            steps_cfg = _parse_steps_arg(args.steps)
        else:
            # 默认阶梯配置
            steps_cfg = [
                (30, args.concurrency, args.qps),
                (30, args.concurrency * 2, args.qps * 2 if args.qps else None),
                (30, args.concurrency * 4, args.qps * 4 if args.qps else None),
            ]

    return LoadTestConfig(
        scenario=scenario,
        load_mode=args.mode,
        duration=args.duration,
        concurrency=args.concurrency,
        qps=args.qps,
        warmup=args.warmup,
        steps=steps_cfg,
        start_concurrency=args.start_concurrency,
        end_concurrency=args.end_concurrency,
        start_qps=args.start_qps,
        end_qps=args.end_qps,
        ramp_duration=args.ramp_duration,
        hold_end_duration=args.hold_end,
        base_duration=args.base_duration,
        spike_duration=args.spike_duration,
        base_concurrency=args.base_concurrency,
        spike_concurrency=args.spike_concurrency,
        base_qps=args.base_qps,
        spike_qps=args.spike_qps,
        spike_count=args.spike_count,
        report_dir=args.report_dir,
        report_name=args.report_name,
        output_console=not args.no_console,
        output_json=not args.no_json,
        output_html=not args.no_html,
        enable_progress_bar=not args.no_progress,
    )


def _cmd_run(args) -> int:
    """执行压测命令"""
    script_path = Path(args.script)
    try:
        scenario = _load_scenario_from_file(script_path)
    except Exception as e:
        print(f"❌ 加载场景失败: {e}", file=sys.stderr)
        return 1

    # 如果是 list 模式，直接列出并退出
    if args.list_only:
        _list_scenario(scenario)
        return 0

    # 构建配置
    try:
        config = _build_config_from_args(args, scenario)
    except Exception as e:
        print(f"❌ 配置错误: {e}", file=sys.stderr)
        return 1

    # 列出场景信息
    if args.verbose:
        _list_scenario(scenario)

    print(f"🚀 开始压测: {scenario.name}")
    print(f"   模式: {args.mode} | 并发: {args.concurrency} | 时长: {args.duration}s" +
          (f" | QPS: {args.qps}" if args.qps else ""))
    print()

    # 执行压测
    engine = LoadTestEngine(config)
    result = engine.run()

    # 返回码：完全成功=0，高错误率=1，压测中断=2
    if result.stopped_early:
        return 2
    if result.metrics.errors.error_rate > 0.05:
        return 1
    return 0


def _cmd_list(args) -> int:
    """列出场景信息"""
    script_path = Path(args.script)
    try:
        scenario = _load_scenario_from_file(script_path)
    except Exception as e:
        print(f"❌ 加载场景失败: {e}", file=sys.stderr)
        return 1
    _list_scenario(scenario)
    return 0


def _cmd_preview(args) -> int:
    """预览场景前N轮的请求详情"""
    script_path = Path(args.script)
    try:
        scenario = _load_scenario_from_file(script_path)
    except Exception as e:
        print(f"  加载场景失败: {e}", file=sys.stderr)
        return 1

    iterations = args.iterations
    num_workers = max(1, getattr(args, 'workers', 1))
    show_params = getattr(args, 'show_params', True)

    sep_w = 78
    print(f"\n  场景预览: {scenario.name}")
    if scenario.base_url:
        print(f"  Base URL: {scenario.base_url}")
    print(f"  Workers:  {num_workers}")
    print(f"  预览:     每个 Worker 跑 {iterations} 轮迭代")
    print(f"  步骤数:   {len([s for s in scenario.steps if s.enabled])}")
    # 步骤权重信息
    steps_with_qps = [(s.name, s.qps_limit) for s in scenario.steps if s.enabled and s.qps_limit]
    steps_with_w = [(s.name, getattr(s, 'weight', None)) for s in scenario.steps if getattr(s, 'weight', None) is not None]
    if steps_with_qps or steps_with_w:
        print(f"  限速配置:")
        for nm, qp in steps_with_qps:
            print(f"    - {nm}: qps_limit={qp}")
        for nm, w in steps_with_w:
            if w is not None:
                print(f"    - {nm}: weight={w}")
    print()

    # 记录 CSV 行使用和计数器进度
    csv_line_usage: Dict[str, set] = {}  # param_name -> set of row indices
    csv_rows_start: Dict[str, dict] = {}  # worker_id, param_name -> start_row
    csv_rows_end: Dict[str, dict] = {}
    counter_start: Dict[str, dict] = {}
    counter_end: Dict[str, dict] = {}

    for wid in range(num_workers):
        wid_str = f"worker-{wid}"
        # 每个 worker 独立参数集（和真实运行一致）
        params = scenario.parameters.clone()
        if hasattr(params, 'set_worker_context'):
            params.set_worker_context(wid_str, num_workers)

        # 记录起始状态
        csv_rows_start[wid_str] = {}
        counter_start[wid_str] = {}
        for pstat in params.get_stats():
            nm = pstat.get('name')
            if pstat.get('type') == 'csv':
                csv_rows_start[wid_str][nm] = pstat.get('current_index', 0)
                if nm not in csv_line_usage:
                    csv_line_usage[nm] = set()
            elif pstat.get('type') == 'counter' or pstat.get('type') == 'CounterParameter':
                counter_start[wid_str][nm] = pstat.get('current_value') or pstat.get('start', 0)

        worker_header_shown = False

        for i in range(1, iterations + 1):
            # 生成这轮的参数
            vars_dict = params.generate()
            context = scenario.create_context(user_id=f"preview-u-{wid}-{i}")
            context.update(vars_dict)

            # 记录 CSV 使用的行
            for pstat in params.get_stats():
                nm = pstat.get('name')
                if pstat.get('type') == 'csv':
                    idx = pstat.get('current_index', 0)
                    total = pstat.get('total_rows_total', 0)
                    # 当前正在使用的索引
                    using_idx = (idx - 1) % max(1, total) if total else 0
                    csv_line_usage.setdefault(nm, set()).add(using_idx)

            if not worker_header_shown:
                print(f"  {'#' * sep_w}")
                print(f"  # Worker: {wid_str}")
                if num_workers > 1:
                    # 显示这个 worker 分到的 CSV 分片
                    for pstat in params.get_stats():
                        if pstat.get('type') == 'csv':
                            avail = pstat.get('total_rows_available', 0)
                            mode = pstat.get('mode') or pstat.get('read_mode')
                            total = pstat.get('total_rows_total', 0)
                            shard_start = pstat.get('_shard_start')
                            shard_end = pstat.get('_shard_end')
                            if avail and mode and mode not in ('SEQUENTIAL', 'RANDOM'):
                                if shard_start is not None and shard_end is not None:
                                    print(f"  #   CSV [{pstat.get('name')}] 分片: 行 {shard_start}-{shard_end} (共{avail}/{total}行)")
                                else:
                                    print(f"  #   CSV [{pstat.get('name')}] 分片: 共{avail}/{total}行可用 (模式: {mode})")
                            else:
                                print(f"  #   CSV [{pstat.get('name')}] 模式: {mode} ({total}行)")
                    for nm, v in counter_start[wid_str].items():
                        print(f"  #   计数器 [{nm}] 起始: {v}")
                worker_header_shown = True

            print(f"  {'=' * (sep_w - 2)}")
            print(f"    第 {i} 轮迭代 (Iteration {i})")
            # 显示这轮参数值（可选）
            if show_params and vars_dict:
                preview_vals = []
                for pk, pv in vars_dict.items():
                    if isinstance(pv, dict):
                        # 只显示前几列
                        items = list(pv.items())
                        short = ", ".join(f"{k}={v}" for k, v in items[:4])
                        if len(items) > 4:
                            short += "..."
                        preview_vals.append(f"{pk}={{{short}}}")
                    else:
                        preview_vals.append(f"{pk}={pv}")
                joined = ", ".join(preview_vals)
                if len(joined) > 150:
                    joined = joined[:147] + "..."
                print(f"    参数: {joined}")
            print(f"  {'=' * (sep_w - 2)}")

            for j, step in enumerate(scenario.steps, 1):
                if not step.enabled:
                    print(f"\n    > Step {j}: {step.name}  [跳过]")
                    continue

                # 调用 request.execute 获得展开后的请求
                request_result = step.request.execute(context.variables, scenario.default_headers)

                print(f"\n    > Step {j}: {step.name}")
                print(f"      请求名: {request_result.request_name}")
                print(f"      方法:   {request_result.method}")
                print(f"      URL:    {request_result.url}")

                if request_result.headers:
                    print(f"      Headers:")
                    for k, v in request_result.headers.items():
                        print(f"        {k}: {v}")
                else:
                    print(f"      Headers: (无)")

                if request_result.body is not None:
                    body_str = str(request_result.body)
                    if len(body_str) > 500:
                        body_str = body_str[:500] + "..."
                    print(f"      Body:   {body_str}")
                else:
                    print(f"      Body:   (无)")

                if step.think_time:
                    print(f"      Think:  {step.think_time}s")

            print()

        # 记录结束状态
        csv_rows_end[wid_str] = {}
        counter_end[wid_str] = {}
        for pstat in params.get_stats():
            nm = pstat.get('name')
            if pstat.get('type') == 'csv':
                csv_rows_end[wid_str][nm] = pstat.get('current_index', 0)
            elif pstat.get('type') == 'counter' or pstat.get('type') == 'CounterParameter':
                counter_end[wid_str][nm] = pstat.get('current_value') or pstat.get('start', 0)

    # ====== 数据推进汇总 ======
    print(f"\n  {'#' * sep_w}")
    print(f"  # 数据推进汇总 (Data Progression Summary)")
    print(f"  {'#' * sep_w}")

    # CSV 汇总
    if csv_line_usage:
        print(f"\n    CSV 数据池使用情况:")
        all_csv_stats = None
        # 拿第一份参数集的 meta 信息
        params0 = scenario.parameters.clone()
        for pstat in params0.get_stats():
            if pstat.get('type') == 'csv':
                nm = pstat.get('name')
                if nm in csv_line_usage:
                    total_rows = pstat.get('total_rows_total', 0)
                    used_rows = len(csv_line_usage.get(nm, set()))
                    looped = used_rows > total_rows if total_rows else False
                    pct = (used_rows / max(1, total_rows)) * 100 if total_rows else 0
                    pct_str = f"{pct:.1f}%" if total_rows else "N/A"
                    tag = " [循环复用!]" if looped else ""
                    print(f"      [{nm}] 使用 {used_rows}/{total_rows} 行 ({pct_str}){tag}")
                    # 显示各 worker 消耗行数
                    for wid_str in sorted(csv_rows_start.keys()):
                        s = csv_rows_start[wid_str].get(nm, 0)
                        e = csv_rows_end[wid_str].get(nm, 0)
                        consumed = e - s
                        print(f"        - {wid_str}: 消耗 {consumed} 行 (idx {s} -> {e})")

    # 计数器汇总
    has_counter = False
    for wid_str, cs in counter_end.items():
        if cs:
            has_counter = True
            break
    if has_counter:
        print(f"\n    计数器进度:")
        all_counter_names = set()
        for wid_str in counter_end:
            all_counter_names.update(counter_end[wid_str].keys())
        for nm in sorted(all_counter_names):
            print(f"      [{nm}]")
            for wid_str in sorted(counter_end.keys()):
                s = counter_start.get(wid_str, {}).get(nm)
                e = counter_end.get(wid_str, {}).get(nm)
                if s is not None and e is not None:
                    print(f"        - {wid_str}: {s} -> {e} (增量 {e - s})")

    total_enabled_steps = len([s for s in scenario.steps if s.enabled])
    total_req = num_workers * iterations * total_enabled_steps
    print(f"\n    总计预览请求数: {total_req} (Workers={num_workers} * 迭代={iterations} * 步骤={total_enabled_steps})")
    print(f"  预览完成\n")
    return 0


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("script", help="场景定义Python脚本路径")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细输出")


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器"""
    parser = argparse.ArgumentParser(
        prog="load-tester",
        description="高性能负载测试工具 - 可配置场景、可控并发、实时指标",
    )
    subparsers = parser.add_subparsers(dest="command", help="命令")

    # run 命令
    run_parser = subparsers.add_parser("run", help="执行压测")
    _add_common_arguments(run_parser)

    # 负载模式
    run_parser.add_argument("--mode", choices=["constant", "step", "ramp", "spike"],
                           default="constant", help="负载模式 (默认: constant)")

    # 恒定模式参数
    run_parser.add_argument("-d", "--duration", type=float, default=60.0,
                           help="压测总时长(秒) (默认: 60)")
    run_parser.add_argument("-c", "--concurrency", type=int, default=10,
                           help="并发用户数 (默认: 10)")
    run_parser.add_argument("-q", "--qps", type=float, default=None,
                           help="目标QPS (默认: 不限)")
    run_parser.add_argument("-w", "--warmup", type=float, default=5.0,
                           help="预热时长(秒) (默认: 5)")

    # 阶梯模式参数
    run_parser.add_argument("--steps", type=str, default=None,
                           help='阶梯配置: "时长,并发,QPS;时长,并发,QPS;..." (step模式)')

    # Ramp模式参数
    run_parser.add_argument("--start-concurrency", type=int, default=1,
                           help="起始并发数 (ramp模式)")
    run_parser.add_argument("--end-concurrency", type=int, default=100,
                           help="结束并发数 (ramp模式)")
    run_parser.add_argument("--start-qps", type=float, default=None,
                           help="起始QPS (ramp模式)")
    run_parser.add_argument("--end-qps", type=float, default=None,
                           help="结束QPS (ramp模式)")
    run_parser.add_argument("--ramp-duration", type=float, default=300.0,
                           help="渐增时长(秒) (ramp模式)")
    run_parser.add_argument("--hold-end", type=float, default=30.0,
                           help="峰值保持时长(秒) (ramp/spike模式)")

    # Spike模式参数
    run_parser.add_argument("--base-duration", type=float, default=60.0,
                           help="基线时长(秒) (spike模式)")
    run_parser.add_argument("--spike-duration", type=float, default=30.0,
                           help="尖峰时长(秒) (spike模式)")
    run_parser.add_argument("--base-concurrency", type=int, default=10,
                           help="基线并发 (spike模式)")
    run_parser.add_argument("--spike-concurrency", type=int, default=200,
                           help="尖峰并发 (spike模式)")
    run_parser.add_argument("--base-qps", type=float, default=None,
                           help="基线QPS (spike模式)")
    run_parser.add_argument("--spike-qps", type=float, default=None,
                           help="尖峰QPS (spike模式)")
    run_parser.add_argument("--spike-count", type=int, default=1,
                           help="尖峰次数 (spike模式)")

    # 报告配置
    run_parser.add_argument("--report-dir", type=str, default="./reports",
                           help="报告输出目录 (默认: ./reports)")
    run_parser.add_argument("--report-name", type=str, default="loadtest_report",
                           help="报告文件名 (默认: loadtest_report)")
    run_parser.add_argument("--no-console", action="store_true",
                           help="不输出控制台报告")
    run_parser.add_argument("--no-json", action="store_true",
                           help="不生成JSON报告")
    run_parser.add_argument("--no-html", action="store_true",
                           help="不生成HTML报告")
    run_parser.add_argument("--no-progress", action="store_true",
                           help="不显示实时进度条")
    run_parser.add_argument("--list-only", action="store_true",
                           help="只列出场景信息，不执行")

    # list 命令
    list_parser = subparsers.add_parser("list", help="列出场景中的步骤和参数")
    _add_common_arguments(list_parser)

    # preview 命令
    preview_parser = subparsers.add_parser("preview", help="预览前N轮请求（展开参数，不实际发送）")
    _add_common_arguments(preview_parser)
    preview_parser.add_argument("-n", "--iterations", type=int, default=3,
                                help="预览迭代轮数 (默认: 3)")
    preview_parser.add_argument("-w", "--workers", type=int, default=1,
                                help="模拟的 Worker 数量 (默认: 1)")
    preview_parser.add_argument("--hide-params", dest="show_params", action="store_false", default=True,
                                help="不显示每轮参数值摘要")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI主入口"""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        return _cmd_run(args)
    elif args.command == "list":
        return _cmd_list(args)
    elif args.command == "preview":
        return _cmd_preview(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
