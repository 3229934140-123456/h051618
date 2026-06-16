"""快速调试：直接运行 scenario.run_iteration"""
import sys
import time

sys.path.insert(0, '.')

from load_tester.scenario.scenario import Scenario, ScenarioStep
from load_tester.scenario.request import HttpRequest, HttpMethod, ResponseData


def mock_exec(req):
    time.sleep(0.002)
    return ResponseData(
        status_code=200,
        body='{}',
        headers={},
        duration_ms=2.0,
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

print('Scenario steps:', [s.name for s in scenario.steps])

ctx = scenario.create_context(user_id='test-user-1')
print('\nRunning iteration 1...')
result = scenario.run_iteration(ctx, mock_exec)

print(f'\nResult:')
print(f'  scenario_name: {result.scenario_name}')
print(f'  is_success: {result.is_success}')
print(f'  error: {result.error}')
print(f'  steps: {len(result.steps)}')
for i, step in enumerate(result.steps):
    print(f'  step {i}: {step.step_name}')
    print(f'    is_success: {step.is_success}')
    print(f'    error: {step.error}')
    print(f'    status_code: {step.status_code}')
