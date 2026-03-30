from django.test import TestCase, override_settings
from django.urls import reverse
import json
import time
import os
from django.utils import timezone

from users.models import User
from users.models import UserAIModelConfig
from projects.models import Project
from testcases.models import TestCase as Case, TestCaseStep
from autotest.models import AutoTestExecution, AutoTestStepRecord, AutoTestSchedule
from autotest.services.browser_use_runner import BrowserUseRunner
from autotest.services.stop_policy import StopPolicy
from autotest.services.evidence import EvidenceBuffer
from asgiref.sync import async_to_sync
from autotest.management.commands.autotest_scheduler import Command as SchedulerCommand
from autotest.utils.llm_factory import get_llm_model
from autotest.utils.openai_compat_chat import ChatOpenAICompat


class TransferFileRunnerUnitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="admin", password="pass1234", is_staff=True)
        self.project = Project.objects.create(name="P1", owner=self.user)
        self.case = Case.objects.create(project=self.project, title="C1", creator=self.user)
        self.step = TestCaseStep.objects.create(
            case=self.case,
            step_number=1,
            description="上传文件并提交",
            expected_result="上传成功",
            transfer_file_name="a.txt",
            transfer_file_content_type="text/plain",
            transfer_file_size=11,
            transfer_file_base64="aGVsbG8gd29ybGQ=",
        )
        self.step2 = TestCaseStep.objects.create(
            case=self.case,
            step_number=2,
            description="提交表单",
            expected_result="成功",
            transfer_file_name="b.txt",
            transfer_file_content_type="text/plain",
            transfer_file_size=5,
            transfer_file_base64="aGVsbG8=",
        )
        self.exec = AutoTestExecution.objects.create(case=self.case, status="pending", executor=self.user)

    def test_decode_transfer_file_payload(self):
        runner = BrowserUseRunner(execution_id=self.exec.id)
        payload = runner._get_transfer_file_payload(self.step)
        self.assertTrue(bool(payload))
        self.assertEqual(payload["name"], "a.txt")
        self.assertEqual(payload["mimeType"], "text/plain")

    def test_requires_upload_when_transfer_file_present(self):
        runner = BrowserUseRunner(execution_id=self.exec.id)
        runner._testcase_steps = [self.step2]
        self.assertTrue(runner._case_step_requires_upload_file(2))

    def test_detect_upload_requirement(self):
        runner = BrowserUseRunner(execution_id=self.exec.id)
        runner._testcase_steps = [self.step]
        self.assertTrue(runner._case_step_requires_upload_file(1))

    def test_materialize_transfer_file_to_disk(self):
        runner = BrowserUseRunner(execution_id=self.exec.id)
        p = runner._ensure_transfer_file_disk_path(1, self.step)
        self.assertTrue(bool(p) and os.path.exists(p))

    def test_get_next_pending_transfer_file_step_no(self):
        runner = BrowserUseRunner(execution_id=self.exec.id)
        runner._testcase_steps = [self.step]
        self.assertEqual(runner._get_next_pending_transfer_file_step_no(), 1)
        runner._transfer_file_applied_steps.add(1)
        self.assertEqual(runner._get_next_pending_transfer_file_step_no(), 0)


class UploadSandboxViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin", password="pass1234", is_staff=True)
        self.user = User.objects.create_user(username="u1", password="pass1234", is_staff=False)

    def test_upload_sandbox_admin_ok(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("upload_sandbox"))
        self.assertEqual(resp.status_code, 200)

    def test_upload_sandbox_non_admin_forbidden(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("upload_sandbox"))
        self.assertEqual(resp.status_code, 403)


class BrowserUseLoginDefaultsUnitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="admin", password="pass1234", is_staff=True)
        self.project = Project.objects.create(name="P1", owner=self.user, base_url="localhost:3000", test_accounts="账号：admin\n密码：admin123")
        self.case = Case.objects.create(project=self.project, title="C1", creator=self.user)
        self.exec = AutoTestExecution.objects.create(case=self.case, status="pending", executor=self.user)

    def test_project_login_defaults_normalized(self):
        runner = BrowserUseRunner(execution_id=self.exec.id)
        got = async_to_sync(runner._get_project_login_defaults_async)()
        self.assertEqual(got.get("url"), "http://localhost:3000")
        self.assertEqual(got.get("username"), "admin")
        self.assertEqual(got.get("password"), "admin123")

    def test_project_login_defaults_single_line_accounts(self):
        self.project.test_accounts = "账号：admin 密码：admin123"
        self.project.save()
        runner = BrowserUseRunner(execution_id=self.exec.id)
        got = async_to_sync(runner._get_project_login_defaults_async)()
        self.assertEqual(got.get("username"), "admin")
        self.assertEqual(got.get("password"), "admin123")

    def test_project_login_defaults_json_table(self):
        self.project.test_accounts = '[{"username":"admin","password":"admin123"},{"username":"u2","password":"p2"}]'
        self.project.save()
        runner = BrowserUseRunner(execution_id=self.exec.id)
        got = async_to_sync(runner._get_project_login_defaults_async)()
        self.assertEqual(got.get("username"), "admin")
        self.assertEqual(got.get("password"), "admin123")

    def test_step_login_overrides_extract(self):
        TestCaseStep.objects.create(case=self.case, step_number=1, description="打开浏览器访问 http://localhost:3000/login", expected_result="页面打开")
        TestCaseStep.objects.create(case=self.case, step_number=2, description="用户名: admin", expected_result="输入成功")
        TestCaseStep.objects.create(case=self.case, step_number=3, description="密码: admin123", expected_result="输入成功")
        runner = BrowserUseRunner(execution_id=self.exec.id)
        steps = list(self.case.steps.all().order_by("step_number"))
        got = runner._extract_login_from_steps(steps)
        self.assertEqual(got.get("url"), "http://localhost:3000/login")
        self.assertEqual(got.get("username"), "admin")
        self.assertEqual(got.get("password"), "admin123")

    def test_login_failed_not_triggered_before_attempt(self):
        runner = BrowserUseRunner(execution_id=self.exec.id)
        runner._seen_auth_response = False
        runner._login_attempted = False

        async def fake_get_page():
            return None

        runner._get_active_page_async = fake_get_page
        failed, brief = async_to_sync(runner._check_login_failed_async)()
        self.assertFalse(failed)
        self.assertIn("未尝试登录", brief)


class BrowserUsePreflightDiagnosticsUnitTests(TestCase):
    def test_classify_dns_error(self):
        runner = BrowserUseRunner(execution_id=1)
        info = runner._classify_nav_error("net::ERR_NAME_NOT_RESOLVED at http://dev.intra/")
        self.assertEqual(info.get("category"), "dns")

    def test_classify_proxy_error(self):
        runner = BrowserUseRunner(execution_id=1)
        info = runner._classify_nav_error("net::ERR_PROXY_CONNECTION_FAILED")
        self.assertEqual(info.get("category"), "proxy")

    def test_persistent_profile_path_contains_ids(self):
        runner = BrowserUseRunner(execution_id=1)
        p = runner._default_persistent_profile_dir(12, 34)
        self.assertIn("executor_12", p)
        self.assertIn("project_34", p)


class QARecorderImportUnitTests(TestCase):
    def test_parse_recorder_payload_basic(self):
        from autotest.views import _qa_recorder_to_steps
        payload = {
            "version": "qa-recorder-0.1",
            "start_url": "https://example.com",
            "steps": [
                {"action": "goto", "value": "https://example.com/login"},
                {"action": "type", "by": "text", "selector": "用户名", "value": "u1"},
                {"action": "type", "by": "text", "selector": "密码", "value": "***"},
                {"action": "click", "by": "text", "selector": "登录"},
                {"action": "wait", "value": "500"},
            ],
        }
        got = _qa_recorder_to_steps(json.dumps(payload, ensure_ascii=False))
        self.assertTrue(got)
        self.assertIn("打开页面：https://example.com/login", got[0])


class SmartDataRewriteUnitTests(TestCase):
    def test_smart_rewrite_input_value(self):
        r = BrowserUseRunner(1)
        s, changed = r._smart_data_rewrite_description("在「用户名」输入：admin", expected="")
        self.assertTrue(changed)
        self.assertIn("【智能生成】", s)

    def test_smart_rewrite_select_value(self):
        r = BrowserUseRunner(1)
        s, changed = r._smart_data_rewrite_description("在「角色」选择：管理员", expected="")
        self.assertTrue(changed)
        self.assertIn("【智能生成】", s)

    def test_smart_rewrite_multi_fields(self):
        r = BrowserUseRunner(1)
        s, changed = r._smart_data_rewrite_description("填写用户名：user007，密码：Passw0rd123，电话：123456789", expected="")
        self.assertTrue(changed)
        self.assertIn("用户名：", s)
        self.assertIn("密码：", s)
        self.assertIn("电话：", s)
        self.assertNotIn("user007", s)
        self.assertNotIn("Passw0rd123", s)
        self.assertNotIn("123456789", s)

    def test_smart_rewrite_invalid_phone_when_expected_error(self):
        r = BrowserUseRunner(1)
        s, changed = r._smart_data_rewrite_description("填写手机号：123456789", expected="提示请输入正确的手机号")
        self.assertTrue(changed)
        self.assertIn("无效手机号", s)

    def test_smart_rewrite_space_separated_fields(self):
        r = BrowserUseRunner(1)
        s, changed = r._smart_data_rewrite_description("用户名 user007 密码 Passw0rd123 电话 123456789", expected="")
        self.assertTrue(changed)
        self.assertNotIn("user007", s)
        self.assertNotIn("Passw0rd123", s)
        self.assertNotIn("123456789", s)


class SubmitLikeDetectUnitTests(TestCase):
    def test_submit_like_detects_by_thought(self):
        r = BrowserUseRunner(1)
        self.assertTrue(r._is_submit_like_action("点击元素索引: 10", "{\"click_element_by_index\": {\"index\": 10}}", "准备点击保存按钮"))


class SaveNoPromptStopUnitTests(TestCase):
    def test_save_like_stops_after_two_no_prompt(self):
        r = BrowserUseRunner(1)
        self.assertFalse(r._record_save_like_observation(prompt_seen=False, response_seen=False, save_effect="u=a|d=b"))
        self.assertTrue(r._record_save_like_observation(prompt_seen=False, response_seen=False, save_effect="u=a|d=b"))

    def test_save_like_resets_when_prompt_seen(self):
        r = BrowserUseRunner(1)
        r._record_save_like_observation(prompt_seen=False, response_seen=True, save_effect="x")
        self.assertEqual(int(r._save_like_no_prompt_clicks or 0), 1)
        r._record_save_like_observation(prompt_seen=True, response_seen=True, save_effect="x")
        self.assertEqual(int(r._save_like_no_prompt_clicks or 0), 0)

    def test_save_like_counts_even_if_network_seen_but_no_prompt(self):
        r = BrowserUseRunner(1)
        self.assertFalse(r._record_save_like_observation(prompt_seen=False, response_seen=True, save_effect="x"))
        self.assertTrue(r._record_save_like_observation(prompt_seen=False, response_seen=True, save_effect="x"))


class ExpectPhoneErrorUnitTests(TestCase):
    def test_expect_phone_error_includes_please_correct(self):
        r = BrowserUseRunner(1)
        self.assertTrue(r._expect_phone_error("提示请输入正确的手机号"))


class GotoUrlSanitizeUnitTests(TestCase):
    def test_sanitize_keeps_oauth_redirect_uri(self):
        r = BrowserUseRunner(1)
        url = "http://dev.suaa.svwsx.cn/login?client_id=x&redirect_uri=http%3A%2F%2Fdev-eline.svwsx.cn%2F&response_type=code"
        self.assertEqual(r._sanitize_goto_url(url), url)

    def test_sanitize_double_scheme(self):
        r = BrowserUseRunner(1)
        self.assertEqual(r._sanitize_goto_url("http://http://a.com/path"), "http://a.com/path")
        self.assertEqual(r._sanitize_goto_url("https://https://a.com"), "https://a.com")


class QARecorderLiveSessionUnitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="pass1234", is_staff=True)
        self.project = Project.objects.create(name="P1", owner=self.user, base_url="https://example.com")

    def test_live_session_new_event_poll(self):
        self.client.login(username="u1", password="pass1234")
        resp = self.client.post(
            "/autotest/record/session/new/",
            data=json.dumps({"project_id": self.project.id}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        token = (resp.json() or {}).get("token")
        self.assertTrue(token)

        resp2 = self.client.post(
            f"/autotest/record/session/{token}/event/",
            data=json.dumps({"step": {"url": "https://example.com", "action": "click", "by": "text", "selector": "登录", "value": ""}}),
            content_type="application/json",
        )
        self.assertEqual(resp2.status_code, 200)

        resp3 = self.client.get(f"/autotest/record/session/{token}/poll/?since=0")
        self.assertEqual(resp3.status_code, 200)
        data3 = resp3.json() or {}
        self.assertTrue(data3.get("success"))
        evs = data3.get("events") or []
        self.assertTrue(isinstance(evs, list) and len(evs) >= 1)

        resp4 = self.client.post(
            f"/autotest/record/session/{token}/commands/set/",
            data=json.dumps({"commands": [{"action": "click", "by": "text", "selector": "登录", "value": ""}]}),
            content_type="application/json",
        )
        self.assertEqual(resp4.status_code, 200)
        data4 = resp4.json() or {}
        self.assertTrue(data4.get("success"))

        resp5 = self.client.get(f"/autotest/record/session/{token}/commands/poll/?since=0")
        self.assertEqual(resp5.status_code, 200)
        data5 = resp5.json() or {}
        self.assertTrue(data5.get("success"))
        self.assertTrue((data5.get("commands") or []))

        resp6 = self.client.post(
            f"/autotest/record/session/{token}/save-case/",
            data=json.dumps({"project_id": self.project.id, "title": "T1"}),
            content_type="application/json",
        )
        self.assertEqual(resp6.status_code, 200)
        data6 = resp6.json() or {}
        self.assertTrue(data6.get("success"))


class BrowserUseLoginDefaultsMoreUnitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="admin", password="pass1234", is_staff=True)
        self.project = Project.objects.create(name="P1", owner=self.user, base_url="localhost:3000", test_accounts="账号：admin\n密码：admin123")
        self.case = Case.objects.create(project=self.project, title="C1", creator=self.user)
        self.exec = AutoTestExecution.objects.create(case=self.case, status="pending", executor=self.user)

    def test_login_failed_only_after_attempt_and_wait(self):
        runner = BrowserUseRunner(execution_id=self.exec.id)
        runner._seen_auth_response = False
        runner._login_attempted = True
        runner._login_attempted_ts = time.time()

        class DummyPage:
            url = "http://localhost:3000/login"

            async def evaluate(self, *_args, **_kwargs):
                return True

        async def fake_get_page():
            return DummyPage()

        runner._get_active_page_async = fake_get_page
        failed, brief = async_to_sync(runner._check_login_failed_async)()
        self.assertFalse(failed)
        self.assertIn("登录尝试中", brief)

        runner._login_attempted_ts = time.time() - 3
        failed2, brief2 = async_to_sync(runner._check_login_failed_async)()
        self.assertTrue(failed2)
        self.assertIn("仍停留在登录页", brief2)

    def test_extract_login_ignores_non_login_user_password(self):
        TestCaseStep.objects.create(case=self.case, step_number=1, description="打开系统首页 http://localhost:3000/", expected_result="页面打开")
        TestCaseStep.objects.create(case=self.case, step_number=2, description="新增用户：用户名：user007 密码：Passw0rd123，电话：123456789", expected_result="新增成功")
        runner = BrowserUseRunner(execution_id=self.exec.id)
        steps = list(self.case.steps.all().order_by("step_number"))
        got = runner._extract_login_from_steps(steps)
        self.assertEqual(got.get("username"), "")
        self.assertEqual(got.get("password"), "")


class ExpectedAssertionStopUnitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="admin", password="pass1234", is_staff=True)
        self.project = Project.objects.create(name="P1", owner=self.user, base_url="localhost:3000", test_accounts='[{"username":"admin","password":"admin123"}]')
        self.case = Case.objects.create(project=self.project, title="C1", creator=self.user)
        TestCaseStep.objects.create(case=self.case, step_number=1, description="新增用户，输入错误手机号并保存", expected_result="提示手机号错误")
        self.exec = AutoTestExecution.objects.create(case=self.case, status="pending", executor=self.user)

    def test_negative_expected_but_success_should_fail_and_create_bug(self):
        runner = BrowserUseRunner(execution_id=self.exec.id)
        runner._testcase_steps = list(self.case.steps.all().order_by("step_number"))
        runner._runtime_messages = ["新增成功"]
        ok, summary, bug_id = async_to_sync(runner._assert_expected_for_case_step_async)(1, 10)
        self.assertFalse(ok)
        self.assertIn("疑似新增/保存成功", summary)
        self.assertTrue(bool(bug_id))

    def test_non_strict_should_not_fail_on_missing_phrases(self):
        runner = BrowserUseRunner(execution_id=self.exec.id)
        runner._testcase_steps = list(self.case.steps.all().order_by("step_number"))
        runner._runtime_messages = []
        ok, summary, bug_id = async_to_sync(runner._assert_expected_for_case_step_async)(1, 10, strict=False)
        self.assertTrue(ok)
        self.assertTrue(isinstance(summary, str))
        self.assertFalse(bool(bug_id))

    def test_normalize_steps_after_assert_failed_should_skip_following(self):
        runner = BrowserUseRunner(execution_id=self.exec.id)
        for i in range(1, 7):
            AutoTestStepRecord.objects.update_or_create(
                execution=self.exec,
                step_number=i,
                defaults={"description": f"AI{i}", "status": "success", "error_message": "", "ai_thought": "", "action_script": "", "metrics": {}},
            )
        runner._stop_reason = "assert_failed"
        runner._forced_bug_id = 123
        runner._assert_failed_step_number = 5
        runner._assert_failed_summary = "断言失败摘要"
        async_to_sync(runner._normalize_steps_after_assert_failed_async)()
        s5 = AutoTestStepRecord.objects.get(execution=self.exec, step_number=5)
        s6 = AutoTestStepRecord.objects.get(execution=self.exec, step_number=6)
        self.assertEqual(s5.status, "failed")
        self.assertIn("BUG-123", s5.error_message)
        self.assertEqual(s6.status, "skipped")

    def test_normalize_steps_after_non_blocking_bug_should_skip_following(self):
        runner = BrowserUseRunner(execution_id=self.exec.id)
        for i in range(1, 7):
            AutoTestStepRecord.objects.update_or_create(
                execution=self.exec,
                step_number=i,
                defaults={"description": f"AI{i}", "status": "success", "error_message": "", "ai_thought": "", "action_script": "", "metrics": {}},
            )
        runner._stop_reason = "non_blocking_bug"
        runner._forced_bug_id = 123
        runner._assert_failed_step_number = 5
        runner._assert_failed_summary = "非阻塞升级摘要"
        async_to_sync(runner._normalize_steps_after_assert_failed_async)()
        s5 = AutoTestStepRecord.objects.get(execution=self.exec, step_number=5)
        s6 = AutoTestStepRecord.objects.get(execution=self.exec, step_number=6)
        self.assertEqual(s5.status, "failed")
        self.assertIn("BUG-123", s5.error_message)
        self.assertEqual(s6.status, "skipped")

    def test_is_submit_like_action(self):
        runner = BrowserUseRunner(execution_id=self.exec.id)
        self.assertTrue(runner._is_submit_like_action("点击【保存】按钮", '{"click_element_by_index": {"index": 7}}', ""))
        self.assertTrue(runner._is_submit_like_action("Clicked button \"提交\"", "", ""))
        self.assertTrue(runner._is_submit_like_action("点击【登录】按钮", '{"click_element_by_index": {"index": 2}}', ""))
        self.assertFalse(runner._is_submit_like_action("点击【+ 新增用户】按钮", '{"click_element_by_index": {"index": 5}}', ""))
        self.assertFalse(runner._is_submit_like_action("向下滚动", '{"scroll_down": {"amount": 700}}', ""))


class StopPolicyUnitTests(TestCase):
    def test_from_settings_and_overrides(self):
        class DummySettings:
            AI_EXEC_SUBMIT_OBSERVE_WAIT_MS = 900
            AI_EXEC_STOP_CHECK_MIN_STEP = 2
            AI_EXEC_NON_BLOCKING_NOTE_MAX = 20
            AI_EXEC_ESCALATE_NON_BLOCKING_ON_STEP_DONE = False

        policy = StopPolicy.from_settings_and_overrides(
            DummySettings(),
            {
                "submit_observation_wait_ms": 1200,
                "stop_check_min_step": 3,
                "non_blocking_note_max": 9,
                "escalate_non_blocking_on_step_done": True,
            },
        )
        self.assertEqual(policy.submit_wait_ms(), 1200)
        self.assertFalse(policy.should_run_stop_check(2))
        self.assertTrue(policy.should_run_stop_check(3))
        self.assertEqual(policy.config.non_blocking_note_max, 9)
        self.assertTrue(policy.should_escalate_non_blocking_on_step_done(True))

    def test_non_blocking_escalation_default_off(self):
        class DummySettings:
            AI_EXEC_SUBMIT_OBSERVE_WAIT_MS = 900
            AI_EXEC_STOP_CHECK_MIN_STEP = 2
            AI_EXEC_NON_BLOCKING_NOTE_MAX = 20
            AI_EXEC_ESCALATE_NON_BLOCKING_ON_STEP_DONE = False

        policy = StopPolicy.from_settings(DummySettings())
        self.assertFalse(policy.should_escalate_non_blocking_on_step_done(True))


class RunnerStabilitySummaryUnitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="admin", password="pass1234", is_staff=True)
        self.project = Project.objects.create(name="P1", owner=self.user)
        self.case = Case.objects.create(project=self.project, title="C1", creator=self.user)
        self.exec = AutoTestExecution.objects.create(case=self.case, status="pending", executor=self.user)

    def test_classify_failure_reason_codes(self):
        runner = BrowserUseRunner(execution_id=self.exec.id)
        c1, g1 = runner._classify_failure_reason("http_502", "failed")
        self.assertEqual(c1, "http_5xx")
        self.assertEqual(g1, "network")
        c2, g2 = runner._classify_failure_reason("submit_no_effect", "failed")
        self.assertEqual(c2, "no_effect")
        self.assertEqual(g2, "interaction")
        c3, g3 = runner._classify_failure_reason("", "completed")
        self.assertEqual(c3, "success")
        self.assertEqual(g3, "success")

    def test_first_failed_step_no(self):
        runner = BrowserUseRunner(execution_id=self.exec.id)
        AutoTestStepRecord.objects.create(execution=self.exec, step_number=1, description="s1", status="success")
        AutoTestStepRecord.objects.create(execution=self.exec, step_number=2, description="s2", status="failed")
        AutoTestStepRecord.objects.create(execution=self.exec, step_number=4, description="s4", status="failed")
        got = async_to_sync(runner._first_failed_step_no_async)()
        self.assertEqual(got, 2)


class EvidenceBufferUnitTests(TestCase):
    def test_evidence_buffer_snapshot_and_last_texts(self):
        buf = EvidenceBuffer(maxlen=5)
        buf.add("toast", "A")
        buf.add("toast", "B")
        buf.add("network", "N1")
        snap = buf.snapshot(10)
        self.assertTrue(len(snap) >= 3)
        self.assertEqual(buf.last_texts("toast", 1), ["B"])


class AutoTestScheduleUnitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="admin", password="pass1234", is_staff=True)
        self.project = Project.objects.create(name="P1", owner=self.user)
        self.case = Case.objects.create(project=self.project, title="C1", creator=self.user)

    def test_schedule_compute_next_interval(self):
        now = timezone.now()
        s = AutoTestSchedule.objects.create(
            project=self.project,
            name="S1",
            enabled=True,
            schedule_type="interval",
            interval_minutes=30,
            case_ids=[self.case.id],
            created_by=self.user,
            next_run_at=now,
        )
        nxt = s.compute_next_run_at(now)
        self.assertTrue(nxt > now)

    def test_scheduler_runs_once(self):
        now = timezone.now()
        s = AutoTestSchedule.objects.create(
            project=self.project,
            name="S2",
            enabled=True,
            schedule_type="once",
            case_ids=[self.case.id],
            created_by=self.user,
            next_run_at=now,
        )
        SchedulerCommand()._run_one(s.id, now)
        ex = AutoTestExecution.objects.filter(schedule_id=s.id).first()
        self.assertTrue(bool(ex))
        s.refresh_from_db()
        self.assertFalse(s.enabled)


class CITriggerApiUnitTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username="admin", password="pass1234", email="admin@example.com")
        self.project = Project.objects.create(name="P1", owner=self.admin)
        self.case = Case.objects.create(project=self.project, title="C1", creator=self.admin)

    @override_settings(AI_EXEC_CI_TOKEN="t1")
    def test_ci_trigger_creates_execution(self):
        resp = self.client.post(
            "/autotest/ci/trigger/",
            data='{"case_ids":[%d]}' % self.case.id,
            content_type="application/json",
            **{"HTTP_X_CI_TOKEN": "t1"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("success"))
        ex_ids = data.get("execution_ids") or []
        self.assertTrue(bool(ex_ids))
        ex = AutoTestExecution.objects.get(id=int(ex_ids[0]))
        self.assertEqual(ex.trigger_source, "ci")


class CITriggerUserTokenUnitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="pass1234", is_staff=True)
        self.project = Project.objects.create(name="P1", owner=self.user)
        self.case = Case.objects.create(project=self.project, title="C1", creator=self.user, execution_type=2)
        from users.models import UserCICredential
        self.cred = UserCICredential.objects.create(user=self.user, token="tok1", enabled=True)

    def test_ci_trigger_user_token_authorized(self):
        resp = self.client.post(
            "/autotest/ci/trigger/",
            data='{"case_ids":[%d]}' % self.case.id,
            content_type="application/json",
            **{"HTTP_X_CI_TOKEN": "tok1"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("success"))

    def test_ci_trigger_schedule_scoped_to_user(self):
        s = AutoTestSchedule.objects.create(
            project=self.project,
            name="S1",
            enabled=True,
            schedule_type="interval",
            interval_minutes=10,
            case_ids=[self.case.id],
            created_by=self.user,
            next_run_at=timezone.now(),
        )
        other = User.objects.create_user(username="u2", password="pass1234", is_staff=True)
        from users.models import UserCICredential
        UserCICredential.objects.create(user=other, token="tok2", enabled=True)

        resp = self.client.post(
            "/autotest/ci/trigger/",
            data='{"schedule_id":%d}' % s.id,
            content_type="application/json",
            **{"HTTP_X_CI_TOKEN": "tok2"},
        )
        self.assertEqual(resp.status_code, 404)


class SchedulePagePermissionUnitTests(TestCase):
    def setUp(self):
        self.u1 = User.objects.create_user(username="u1", password="pass1234", is_staff=True)
        self.u2 = User.objects.create_user(username="u2", password="pass1234", is_staff=True)
        self.p1 = Project.objects.create(name="P1", owner=self.u1)
        self.c1 = Case.objects.create(project=self.p1, title="C1", creator=self.u1, execution_type=2)
        self.s1 = AutoTestSchedule.objects.create(
            project=self.p1,
            name="S1",
            enabled=True,
            schedule_type="interval",
            interval_minutes=10,
            case_ids=[self.c1.id],
            created_by=self.u1,
            next_run_at=timezone.now(),
        )

    def test_only_owner_can_edit(self):
        self.client.login(username="u2", password="pass1234")
        resp = self.client.get(f"/autotest/schedules/{self.s1.id}/edit/")
        self.assertEqual(resp.status_code, 404)


class EngineSelectionUnitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="pass1234", is_staff=True)
        self.project = Project.objects.create(name="P1", owner=self.user, base_url="https://www.baidu.com")
        self.case = Case.objects.create(project=self.project, title="C1", creator=self.user, execution_type=2)

    @override_settings(AI_EXEC_ENGINE="browser_use")
    def test_build_runner_switches_by_payload(self):
        ex = AutoTestExecution.objects.create(case=self.case, status="pending", executor=self.user, trigger_payload={"engine": "playwright_ai"})
        from autotest.services.execution_queue import build_runner
        r = build_runner(ex)
        self.assertEqual(r.__class__.__name__, "BrowserUseRunner")

    def test_record_page_ok(self):
        self.client.login(username="u1", password="pass1234")
        resp = self.client.get("/autotest/record/")
        self.assertEqual(resp.status_code, 200)


class ExecutionStatusViewUnitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u_status", password="pass1234", is_staff=True)
        self.project = Project.objects.create(name="P_STATUS", owner=self.user)
        self.case = Case.objects.create(project=self.project, title="C_STATUS", creator=self.user, execution_type=2)
        self.exec = AutoTestExecution.objects.create(case=self.case, status="running", executor=self.user)

    @override_settings(AI_EXEC_SCREENSHOT_MODE="all")
    def test_status_contains_screenshot_mode(self):
        self.client.login(username="u_status", password="pass1234")
        resp = self.client.get(f"/autotest/status/{self.exec.id}/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("screenshot_mode"), "all")


class ExecHeadlessToggleViewUnitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="pass1234", is_staff=True)
        self.project = Project.objects.create(name="P1", owner=self.user, base_url="https://example.com")
        self.case = Case.objects.create(project=self.project, title="C1", creator=self.user, execution_type=2)
        UserAIModelConfig.objects.create(
            user=self.user,
            exec_provider="qwen",
            exec_model="qwen-vl-plus",
            exec_api_key="dummy",
            exec_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

    @override_settings(AI_EXEC_TOAST_OCR_ENABLED=False, AI_EXEC_GUIDE_HINT_ENABLED=False)
    def test_run_test_accepts_headless_override(self):
        self.client.login(username="u1", password="pass1234")
        resp = self.client.post(
            f"/autotest/run/{self.case.id}/",
            data=json.dumps({"headless": False}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(bool(data.get("success")))
        ex_id = int(data.get("execution_id") or 0)
        ex = AutoTestExecution.objects.get(id=ex_id)
        self.assertEqual(ex.trigger_payload.get("headless"), False)

    @override_settings(AI_EXEC_TOAST_OCR_ENABLED=False, AI_EXEC_GUIDE_HINT_ENABLED=False)
    def test_batch_run_accepts_headless_override(self):
        self.client.login(username="u1", password="pass1234")
        resp = self.client.post(
            "/autotest/batch-run/",
            data=json.dumps({"case_ids": [self.case.id], "headless": False}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(bool(data.get("success")))
        ex_ids = list(data.get("execution_ids") or [])
        self.assertTrue(bool(ex_ids))
        ex = AutoTestExecution.objects.get(id=int(ex_ids[0]))
        self.assertEqual(ex.trigger_payload.get("headless"), True)


class KimiTemperatureUnitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="pass1234", is_staff=True)
        UserAIModelConfig.objects.create(
            user=self.user,
            exec_provider="kimi",
            exec_model="kimi-k2.5",
            exec_api_key="dummy",
            exec_base_url="https://api.moonshot.cn/v1",
        )

    def test_kimi_forces_temperature_one(self):
        llm = get_llm_model(temperature=0.0, user=self.user)
        self.assertEqual(getattr(llm, "temperature", None), 1.0)
        self.assertEqual(llm.__class__.__name__, "ChatOpenAICompat")


class KimiJsonFenceParseUnitTests(TestCase):
    def test_parses_code_fence_json(self):
        from pydantic import BaseModel
        from autotest.utils.openai_compat_chat import _extract_first_json_value

        class M(BaseModel):
            a: int

        raw = "```json\n{\n  \"a\": 1\n}\n```\nTrailing text"
        obj = _extract_first_json_value(raw)
        parsed = M.model_validate(obj)
        self.assertEqual(parsed.a, 1)


class OpenAICompatUsageUnitTests(TestCase):
    def test_normalize_usage_fills_missing_fields(self):
        from browser_use.llm.views import ChatInvokeCompletion
        from autotest.utils.openai_compat_chat import _normalize_usage

        class Usage:
            prompt_tokens = 1
            completion_tokens = 2
            total_tokens = 3

        u = _normalize_usage(Usage())
        self.assertEqual(u.prompt_tokens, 1)
        self.assertTrue(hasattr(u, "prompt_cached_tokens"))
        self.assertTrue(hasattr(u, "prompt_cache_creation_tokens"))
        self.assertTrue(hasattr(u, "prompt_image_tokens"))
        ChatInvokeCompletion(completion="ok", usage=u)

    def test_normalize_usage_accepts_dict(self):
        from browser_use.llm.views import ChatInvokeCompletion
        from autotest.utils.openai_compat_chat import _normalize_usage

        u = _normalize_usage({"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})
        self.assertEqual(u.total_tokens, 3)
        ChatInvokeCompletion(completion="ok", usage=u)

    def test_chat_invoke_completion_usage_dict_backward_compat(self):
        from browser_use.llm.views import ChatInvokeCompletion, ChatInvokeUsage

        if ChatInvokeUsage.model_fields["prompt_cached_tokens"].is_required():
            self.skipTest("browser_use ChatInvokeUsage 仍要求必须提供缓存/图片字段键")

        ChatInvokeCompletion(
            completion="ok",
            usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        )


class AgentOutputCompatUnitTests(TestCase):
    def test_agent_output_wait_time_normalized(self):
        from browser_use.agent.views import AgentOutput
        from browser_use.tools.service import Tools
        from autotest.utils.openai_compat_chat import _normalize_agent_output_obj

        tools = Tools()
        action_model = tools.registry.create_action_model()
        Output = AgentOutput.type_with_custom_actions(action_model)
        raw = {"evaluation_previous_goal": "", "memory": "", "next_goal": "", "action": [{"wait": {"time": 2}}]}
        fixed = _normalize_agent_output_obj(raw)
        Output.model_validate(fixed)

    def test_agent_output_wait_duration_normalized(self):
        from browser_use.agent.views import AgentOutput
        from browser_use.tools.service import Tools
        from autotest.utils.openai_compat_chat import _normalize_agent_output_obj

        tools = Tools()
        action_model = tools.registry.create_action_model()
        Output = AgentOutput.type_with_custom_actions(action_model)
        raw = {"evaluation_previous_goal": "", "memory": "", "next_goal": "", "action": [{"wait": {"duration": 2}}]}
        fixed = _normalize_agent_output_obj(raw)
        Output.model_validate(fixed)

    def test_agent_output_click_element_normalized(self):
        from browser_use.agent.views import AgentOutput
        from browser_use.tools.service import Tools
        from autotest.utils.openai_compat_chat import _normalize_agent_output_obj

        tools = Tools()
        action_model = tools.registry.create_action_model()
        Output = AgentOutput.type_with_custom_actions(action_model)
        raw = {"evaluation_previous_goal": "", "memory": "", "next_goal": "", "action": [{"click": {"element": "1913"}}]}
        fixed = _normalize_agent_output_obj(raw)
        got = Output.model_validate(fixed)
        self.assertEqual(got.action[0].get_index(), 1913)

    def test_agent_output_send_keys_normalized(self):
        from browser_use.agent.views import AgentOutput
        from browser_use.tools.service import Tools
        from autotest.utils.openai_compat_chat import _normalize_agent_output_obj

        tools = Tools()
        action_model = tools.registry.create_action_model()
        Output = AgentOutput.type_with_custom_actions(action_model)
        raw = {"evaluation_previous_goal": "", "memory": "", "next_goal": "", "action": [{"send_keys": {"keys": ["Enter"]}}]}
        fixed = _normalize_agent_output_obj(raw)
        self.assertEqual((fixed.get("action") or [{}])[0], {"send_keys": {"keys": "Enter"}})
        raw2 = {"evaluation_previous_goal": "", "memory": "", "next_goal": "", "action": [{"send_keys": {"key": "Enter"}}]}
        fixed2 = _normalize_agent_output_obj(raw2)
        self.assertEqual((fixed2.get("action") or [{}])[0], {"send_keys": {"keys": "Enter"}})
        Output.model_validate(fixed)

    def test_agent_output_input_element_index_normalized(self):
        from browser_use.agent.views import AgentOutput
        from browser_use.tools.service import Tools
        from autotest.utils.openai_compat_chat import _normalize_agent_output_obj

        tools = Tools()
        action_model = tools.registry.create_action_model()
        Output = AgentOutput.type_with_custom_actions(action_model)
        raw = {"evaluation_previous_goal": "", "memory": "", "next_goal": "", "action": [{"input": {"element_index": 24, "text": "abc"}}]}
        fixed = _normalize_agent_output_obj(raw)
        self.assertEqual((fixed.get("action") or [{}])[0], {"input": {"index": 24, "text": "abc"}})
        Output.model_validate(fixed)

    def test_agent_output_mixed_legacy_actions_normalized(self):
        from browser_use.agent.views import AgentOutput
        from browser_use.tools.service import Tools
        from autotest.utils.openai_compat_chat import _normalize_agent_output_obj

        tools = Tools()
        action_model = tools.registry.create_action_model()
        Output = AgentOutput.type_with_custom_actions(action_model)
        raw = {
            "evaluation_previous_goal": "",
            "memory": "",
            "next_goal": "",
            "action": [
                {"input": {"element_index": 22, "text": "89153554"}},
                {"send_keys": {"key": "Enter"}},
            ],
        }
        fixed = _normalize_agent_output_obj(raw)
        self.assertEqual((fixed.get("action") or [])[0], {"input": {"index": 22, "text": "89153554"}})
        self.assertEqual((fixed.get("action") or [])[1], {"send_keys": {"keys": "Enter"}})
        Output.model_validate(fixed)

    def test_agent_output_wait_timeout_normalized(self):
        from browser_use.agent.views import AgentOutput
        from browser_use.tools.service import Tools
        from autotest.utils.openai_compat_chat import _normalize_agent_output_obj

        tools = Tools()
        action_model = tools.registry.create_action_model()
        Output = AgentOutput.type_with_custom_actions(action_model)
        raw = {"evaluation_previous_goal": "", "memory": "", "next_goal": "", "action": [{"wait": {"timeout": 2}}]}
        fixed = _normalize_agent_output_obj(raw)
        self.assertEqual((fixed.get("action") or [])[0], {"wait": {"seconds": 2}})
        Output.model_validate(fixed)

    def test_agent_output_select_dropdown_timeout_removed(self):
        from browser_use.agent.views import AgentOutput
        from browser_use.tools.service import Tools
        from autotest.utils.openai_compat_chat import _normalize_agent_output_obj

        tools = Tools()
        action_model = tools.registry.create_action_model()
        Output = AgentOutput.type_with_custom_actions(action_model)
        raw = {
            "evaluation_previous_goal": "",
            "memory": "",
            "next_goal": "",
            "action": [{"select_dropdown": {"element_index": 7, "text": "中文", "timeout": 2000}}],
        }
        fixed = _normalize_agent_output_obj(raw)
        self.assertEqual((fixed.get("action") or [])[0], {"select_dropdown": {"index": 7, "text": "中文"}})
        Output.model_validate(fixed)


class ProviderSelectionUnitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="pass1234", is_staff=True)

    def test_qwen_uses_openai_compat_model(self):
        UserAIModelConfig.objects.create(
            user=self.user,
            exec_provider="qwen",
            exec_model="qwen-vl-plus",
            exec_api_key="dummy",
            exec_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        llm = get_llm_model(user=self.user)
        self.assertEqual(llm.__class__.__name__, "ChatOpenAICompat")


class QAToolsUnitTests(TestCase):
    def test_qatools_can_register_actions(self):
        from autotest.utils.qa_tools import QATools

        class _RunnerStub:
            _case_step_last_seen = 0

            def _case_step_requires_upload_file(self, step_no: int) -> bool:
                return False

        QATools(_RunnerStub())


class CaseStepMarkerUnitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="admin", password="pass1234", is_staff=True)
        self.project = Project.objects.create(name="P1", owner=self.user, base_url="localhost:3000")
        self.case = Case.objects.create(project=self.project, title="C1", creator=self.user)
        TestCaseStep.objects.create(case=self.case, step_number=1, description="打开首页")
        TestCaseStep.objects.create(case=self.case, step_number=2, description="执行操作A")
        TestCaseStep.objects.create(case=self.case, step_number=3, description="执行操作B")
        self.exec = AutoTestExecution.objects.create(case=self.case, status="pending", executor=self.user)

    def test_extract_case_step_progress_marks_done(self):
        runner = BrowserUseRunner(execution_id=self.exec.id)
        text = (
            "用例步骤1/3。\n完成用例步骤1（关键证据：已打开首页）。\n"
            "用例步骤2/3。\n完成用例步骤2（关键证据：已完成操作A）。\n"
            "用例步骤3/3。\n步骤3：通过。\n"
        )
        cur, total, done = runner._extract_case_step_progress(text)
        self.assertEqual(total, 3)
        self.assertEqual(sorted(list(done)), [1, 2, 3])
