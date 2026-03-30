import os
import asyncio
import sys
import urllib.parse


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "qa_platform.settings")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _setup_django():
    import django

    django.setup()


async def _run(execution_id: int):
    from autotest.services.browser_use_runner import BrowserUseRunner
    from playwright.async_api import async_playwright

    html = """<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <style>
      body{margin:0;height:100vh;overflow:hidden;background:#111;color:#fff;font-family:Arial}
      #box{width:700px;height:520px;margin:20px auto;border:1px solid #444;overflow:auto}
      .item{padding:16px;border-bottom:1px solid #222}
    </style>
  </head>
  <body>
    <div id="box"><div id="list"></div></div>
    <script>
      let batch = 0;
      const list = document.getElementById('list');
      function add(n){
        for (let i=0;i<n;i++){
          const d=document.createElement('div');
          d.className='item';
          d.textContent='item '+(list.children.length+1);
          list.appendChild(d);
        }
      }
      add(30);
      document.getElementById('box').addEventListener('scroll',()=>{
        const el=document.getElementById('box');
        if (el.scrollTop + el.clientHeight >= el.scrollHeight - 5){
          if (batch < 3){
            batch++;
            setTimeout(()=>add(20),120);
          }
        }
      });
    </script>
  </body>
</html>"""

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(viewport={"width": 900, "height": 600})
    page = await ctx.new_page()
    await page.goto("data:text/html," + urllib.parse.quote(html), wait_until="load")

    r = BrowserUseRunner(int(execution_id))
    r._pw_contexts = [ctx]
    await r._smart_scroll_async(2, until_bottom=True)

    cnt = await page.evaluate("() => document.querySelectorAll('#list .item').length")
    state = await page.evaluate(
        "() => { const el=document.getElementById('box'); return {top: el.scrollTop, height: el.scrollHeight, client: el.clientHeight}; }"
    )
    print("items", cnt, "scroll", state)

    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    _setup_django()
    from django.contrib.auth import get_user_model
    from projects.models import Project
    from testcases.models import TestCase, TestCaseStep
    from autotest.models import AutoTestExecution, AutoTestStepRecord

    U = get_user_model()
    u, _ = U.objects.get_or_create(username="scroll_verify_user", defaults={"password": "pass1234"})
    p = Project.objects.create(name="scroll_verify_project", description="", owner=u, status=1)
    tc = TestCase.objects.create(
        project=p,
        requirement=None,
        title="scroll_verify_case",
        pre_condition="",
        type=1,
        execution_type=2,
        priority=2,
        status=0,
        creator=u,
    )
    step = TestCaseStep.objects.create(case=tc, step_number=1, description="scroll", expected_result="", is_executed=False)
    ex = AutoTestExecution.objects.create(case=tc, executor=u, status="running")
    AutoTestStepRecord.objects.create(execution=ex, step=step, step_number=2, description="scroll", status="success", metrics={})
    asyncio.run(_run(ex.id))
