import os
import sys
import asyncio


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "qa_platform.settings")


def _setup():
    import django

    django.setup()


class DummyAgent:
    def __init__(self):
        self.stopped = False
        self.history = None

    def stop(self):
        self.stopped = True


async def _run(execution_id: int):
    from autotest.services.browser_use_runner import BrowserUseRunner, _AgentManualStop

    r = BrowserUseRunner(int(execution_id))
    a = DummyAgent()
    try:
        await r._apply_control_signals_async(a, 1)
    except _AgentManualStop:
        pass
    print("manual_stop_agent_stopped", a.stopped)

    r2 = BrowserUseRunner(int(execution_id))
    r2._seen_auth_response = True
    r2._last_auth_status = 401
    failed, brief = await r2._check_login_failed_async()
    print("login_failed", failed, "brief", brief)


if __name__ == "__main__":
    _setup()
    from django.contrib.auth import get_user_model
    from projects.models import Project
    from testcases.models import TestCase, TestCaseStep
    from autotest.models import AutoTestExecution

    U = get_user_model()
    u, _ = U.objects.get_or_create(username="verify_stop_user", defaults={"password": "pass1234"})
    p = Project.objects.create(name="verify_stop_project", description="", owner=u, status=1)
    tc = TestCase.objects.create(
        project=p,
        requirement=None,
        title="verify_stop_case",
        pre_condition="",
        type=1,
        execution_type=2,
        priority=2,
        status=0,
        creator=u,
    )
    TestCaseStep.objects.create(case=tc, step_number=1, description="登录", expected_result="应登录成功", is_executed=False)
    ex = AutoTestExecution.objects.create(case=tc, executor=u, status="running", stop_signal=True)
    asyncio.run(_run(ex.id))
