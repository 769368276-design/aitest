from __future__ import annotations

import json
import threading
from typing import AsyncGenerator, List

from ai_assistant.utils.llms import get_text_model_client
from ai_assistant.services.openapi_service import openapi_service
from ai_assistant.services.testcase_generation.types import StreamEvent
from ai_assistant.services.testcase_generation.agents import create_agent, stream_agent
from ai_assistant.services.testcase_generation.postprocess import ensure_markdown_parseable


def _format_ops(api_info: dict) -> str:
    lines: List[str] = []
    for p in api_info.get("paths") or []:
        path = p.get("path") or ""
        for op in p.get("operations") or []:
            lines.append(f"- {op.get('method','').upper()} {path}  {op.get('summary') or ''}".strip())
    return "\n".join(lines)[:6000]


def _prompt(api_info: dict, scenarios: list, context: str, requirements: str) -> str:
    ops = _format_ops(api_info)
    scen = "\n".join([f"- {s}" for s in (scenarios or [])])[:6000]
    return f"""请基于上传的OpenAPI/Swagger文档生成API测试用例（不要啰嗦，不要重复）。

上下文信息: {context}

需求: {requirements}

约束：上下文/需求仅用于补充范围与重点，不要把它们当作“需求文档章节”来生成用例；不要为背景/目标/范围/术语/修订记录/目录/概述等章节本身生成用例。

API概览:
- 标题: {api_info.get('info',{}).get('title','未知')}
- 版本: {api_info.get('info',{}).get('version','未知')}
- 路径数量: {len(api_info.get('paths') or [])}

端点列表（用于覆盖完整性）:
{ops}

测试场景概览（用于覆盖完整性）:
{scen}

覆盖硬约束（每个端点至少覆盖）：\n1) 正向\n2) 鉴权失败/权限不足（若适用）\n3) 参数校验失败/缺参/类型不符\n4) 常见业务错误码与幂等/重复提交（若适用）
编号：从 TC-001 连续递增。

格式约束（必须可解析）：\n1) 每条用例以二级标题开始：## TC-001: 测试标题\n2) 字段（加粗）：**优先级:** 高/中/低；**描述:** 一行；**前置条件:** 无/...\n3) 测试步骤用标准Markdown表格：\n| # | 步骤描述 | 预期结果 |\n| --- | --- | --- |\n| 1 | ... | ... |
"""


class OpenAPIStrategy:
    async def generate(self, file_path: str, context: str, requirements: str, cancel_event: threading.Event | None = None, user=None) -> AsyncGenerator[StreamEvent, None]:
        api_data = openapi_service.parse_openapi_file(file_path)
        api_info = api_data["api_info"]
        scenarios = openapi_service.generate_test_scenarios(api_info)
        agent = create_agent(get_text_model_client(user=user), "你是资深测试工程师。输出严格可解析的Markdown用例。", "openapi_cases_agent")
        prompt = _prompt(api_info, scenarios, context or "", requirements or "")
        yield StreamEvent(type="meta", message="openapi_start")
        parts = []
        async for chunk in stream_agent(agent, prompt, cancel_event=cancel_event):
            if not chunk:
                continue
            parts.append(chunk)
            yield StreamEvent(type="delta", text=chunk)
        raw = "".join(parts)
        fixed, _ = ensure_markdown_parseable(raw, tc_start=1)
        yield StreamEvent(type="final", text=fixed or raw)
        yield StreamEvent(type="done", message="openapi_done")
