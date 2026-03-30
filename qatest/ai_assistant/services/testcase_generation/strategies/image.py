from __future__ import annotations

import threading
from typing import AsyncGenerator

from PIL import Image as PILImage

from ai_assistant.utils.llms import get_vision_model_client
from ai_assistant.services.testcase_generation.types import StreamEvent
from ai_assistant.services.testcase_generation.agents import (
    AGImage,
    AGMultiModalMessage,
    create_agent,
    stream_agent,
)
from ai_assistant.services.testcase_generation.postprocess import ensure_markdown_parseable


def _prompt(context: str, requirements: str) -> str:
    return f"""请基于上传的图像生成测试用例（不要啰嗦，不要重复）。

上下文信息: {context}

需求: {requirements}

约束：上下文/需求仅用于补充范围与重点，不要把它们当作“需求文档章节”来生成用例；不要为背景/目标/范围/术语/修订记录/目录/概述等章节本身生成用例，除非图片中明确存在对应可交互模块/页面/字段。

数量与覆盖：先列“场景要点”，再输出 12–20 条用例（尽量多），覆盖正向/反向/边界/权限/状态流转/异常提示。
编号：从 TC-001 连续递增。

格式约束（必须可解析）：\n1) 每条用例以二级标题开始：## TC-001: 测试标题\n2) 字段（加粗）：**优先级:** 高/中/低；**描述:** 一行；**前置条件:** 无/...\n3) 测试步骤用标准Markdown表格：\n| # | 步骤描述 | 预期结果 |\n| --- | --- | --- |\n| 1 | ... | ... |
"""


class ImageStrategy:
    async def generate(self, file_path: str, context: str, requirements: str, cancel_event: threading.Event | None = None, user=None) -> AsyncGenerator[StreamEvent, None]:
        img = AGImage(PILImage.open(file_path))
        agent = create_agent(get_vision_model_client(user=user), "你是资深测试工程师。输出严格可解析的Markdown用例。", "image_cases_agent")
        prompt = _prompt(context or "", requirements or "")
        mm = AGMultiModalMessage(content=[prompt, img], source="user")
        yield StreamEvent(type="meta", message="image_start")
        parts = []
        async for chunk in stream_agent(agent, mm, cancel_event=cancel_event):
            if not chunk:
                continue
            parts.append(chunk)
            yield StreamEvent(type="delta", text=chunk)
        raw = "".join(parts)
        fixed, _ = ensure_markdown_parseable(raw, tc_start=1)
        yield StreamEvent(type="final", text=fixed or raw)
        yield StreamEvent(type="done", message="image_done")
