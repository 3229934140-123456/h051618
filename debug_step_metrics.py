"""快速调试：看看步骤级统计的问题"""
import sys
import time

sys.path.insert(0, '.')

from load_tester.scenario.scenario import Scenario, ScenarioStep
from load_tester.scenario.request import HttpRequest, HttpMethod, ResponseData
from load_tester.engine import LoadTestEngine, LoadTestConfig


def mock_exec(req):
    time.sleep(0.002)
    return ResponseData(
        status_code=200,
        body='{}',
        headers={},
        latency=0.002,
        timestamp=time.time(),
        error=None,
    )


scenario = Scenario(name='test')
scenario.add_step(ScenarioStep(
    name='step_a',
    request=HttpRequest(name='ra', method=HttpMethod.GET, url='http://test/a'),
))
scenario.add_step(ScenarioStep(
    name='step_b',
    request=HttpRequest(name='rb', method=HttpMethod.GET, url='http://test/b'),
))
scenario.add_step(ScenarioStep(
    name='step_c',
    request=HttpRequest(name='rc', method=HttpMethod.GET, url='http://test/c'),
))

config = LoadTestConfig(
    scenario=scenario,
    load_mode='constant',
    duration=2,
    concurrency=1,
    qps=10,
    output_json=False,
    output_html=False,
    output_console=False,
)

engine = LoadTestEngine(config, custom_http_executor=mock_exec)
result = engine.run()
m = result.metrics

lines = []
lines.append('=== 调试信息 ===')
lines.append(f'Total requests (throughput): {m.throughput.total_requests}')
lines.append(f'by_step keys: {list(m.by_step.keys())}')
for k, v in m.by_step.items():
    lines.append(f'  {k}: total={v.total_requests}, qps={v.overall_qps:.2f}')

lines.append('')
lines.append(f'by_name keys: {list(m.by_name.keys())}')
for k, v in m.by_name.items():
    lines.append(f'  {k}: count={v.count}')

lines.append('')
lines.append('Scenario metrics:')
lines.append(f'  total_iterations: {m.scenario.total_iterations}')
lines.append(f'  success_iterations: {m.scenario.success_iterations}')

with open('debug_output.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))

print('Done. Output written to debug_output.txt')
