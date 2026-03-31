import json
from typing import List, Dict, Any, AsyncGenerator
import re
import asyncio
import os
import threading

try:
    from autogen_agentchat.agents import AssistantAgent
    from autogen_agentchat.base import TaskResult
    from autogen_agentchat.messages import ModelClientStreamingChunkEvent, MultiModalMessage as AGMultiModalMessage
    from autogen_core import Image as AGImage
except Exception:
    AssistantAgent = None
    TaskResult = None
    ModelClientStreamingChunkEvent = None
    AGMultiModalMessage = None
    AGImage = None
from PIL import Image as PILImage
from django.conf import settings

from ai_assistant.utils.llms import get_model_client
from ai_assistant.utils.llms import get_text_model_client, get_vision_model_client

class AIService:
    def __init__(self):
        pass

    def _create_agent(self, selected_model_client, system_message: str) -> AssistantAgent:
        if AssistantAgent is None:
            raise RuntimeError("缺少依赖 autogen-agentchat/autogen-core：请安装后再启用 AI 功能")
        return AssistantAgent(
            name="test_case_agent",
            model_client=selected_model_client,
            system_message=system_message,
            model_client_stream=True,
        )

    async def _stream_agent(self, agent: AssistantAgent, task_message, cancel_event: threading.Event | None = None):
        if ModelClientStreamingChunkEvent is None or TaskResult is None:
            raise RuntimeError("缺少依赖 autogen-agentchat：请安装后再启用 AI 功能")
        stream = agent.run_stream(task=task_message)
        try:
            async for event in stream:
                if cancel_event is not None and cancel_event.is_set():
                    try:
                        await stream.aclose()
                    except Exception:
                        pass
                    break
                if isinstance(event, ModelClientStreamingChunkEvent):
                    yield event.content
                elif isinstance(event, TaskResult):
                    break
        finally:
            try:
                await stream.aclose()
            except Exception:
                pass

    async def _run_agent_to_text(self, agent: AssistantAgent, task_message, cancel_event: threading.Event | None = None) -> str:
        parts: List[str] = []
        async for chunk in self._stream_agent(agent, task_message, cancel_event=cancel_event):
            parts.append(chunk)
        return "".join(parts)

    def _extract_json_array_from_text(self, text: str) -> List[Dict[str, Any]]:
        raw = (text or "").strip()
        if not raw:
            return []
        start = raw.find("[")
        end = raw.rfind("]")
        if start != -1 and end != -1 and end > start:
            raw = raw[start : end + 1]
        raw = raw.strip().strip("`")
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        except Exception:
            return []
        return []

    def _extract_json_object_from_text(self, text: str) -> Dict[str, Any]:
        raw = (text or "").strip()
        if not raw:
            return {}
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            raw = raw[start : end + 1]
        raw = raw.strip().strip("`")
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            return {}
        return {}

    def _clip_text(self, s: str, max_len: int) -> str:
        t = (s or "").strip()
        if len(t) <= int(max_len):
            return t
        return t[: int(max_len)] + "\n...(内容过长，已截断)"

    def _norm_compact(self, s: str) -> str:
        return re.sub(r"\s+", "", (s or "").strip())

    def _quote_in_text(self, quote: str, text: str) -> bool:
        q = (quote or "").strip()
        if not q:
            return False
        if q in (text or ""):
            return True
        qn = self._norm_compact(q)
        tn = self._norm_compact(text or "")
        if not qn or not tn:
            return False
        if qn in tn:
            return True
        if len(qn) >= 24 and qn[:24] in tn:
            return True
        if len(qn) >= 16 and qn[:16] in tn:
            return True
        return False

    def _find_last_tc_no(self, text: str) -> int:
        raw = text or ""
        hits = re.findall(r"\bTC-(\d{1,6})\b", raw, flags=re.IGNORECASE)
        if not hits:
            return 0
        try:
            return max(int(x) for x in hits)
        except Exception:
            return 0

    async def get_chat_response(self, prompt: str, user=None) -> str:
        """
        Get a simple chat response from the LLM (Qwen).
        """
        messages = [{"role": "user", "content": prompt}]
        response = await get_model_client(user=user).create(messages)
        return response.content

    def _get_model_client_for_file_type(self, file_path: str, user=None):
        """
        根据文件类型选择合适的模型客户端
        """
        file_extension = file_path.lower().split('.')[-1] if '.' in file_path else ''
        if file_extension in ['png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp']:
            return get_vision_model_client(user=user)
        return get_text_model_client(user=user)

    async def generate_test_cases_stream(
        self,
        file_path: str,
        context: str,
        requirements: str,
        cancel_event: threading.Event | None = None,
        user=None,
    ) -> AsyncGenerator[str, None]:
        """
        基于文件分析、上下文和需求生成测试用例（智能选择模型客户端）
        """
        engine_mode = (getattr(settings, "AI_TESTCASE_ENGINE", "new") or "new").strip().lower()
        if engine_mode != "legacy":
            from ai_assistant.services.testcase_generation import TestCaseGenerationEngine

            engine = TestCaseGenerationEngine()
            async for ev in engine.generate(file_path, context, requirements, cancel_event=cancel_event, user=user):
                if ev.type in ("delta", "progress", "error"):
                    yield ev.text or (f"\n\n**错误**: {ev.message}\n" if ev.type == "error" else "")
            return

        # 根据文件类型选择合适的模型客户端
        selected_model_client = self._get_model_client_for_file_type(file_path, user=user)
        file_extension = file_path.lower().split('.')[-1] if '.' in file_path else ''

        system_message = "你是资深测试工程师。基于输入材料生成测试用例：简洁、去重、覆盖充分、步骤可执行。"
        if file_extension == "pdf":
            from ai_assistant.services.testcase_pipeline import TestCasePipeline
            pipeline = TestCasePipeline(user=user)
            async for chunk in pipeline.generate_from_pdf_stream(file_path, context, requirements, cancel_event=cancel_event):
                yield chunk
            return
        scenario_driven_quantity = """数量要求（不固定条数）：
1) 先从材料中抽取《功能场景清单》（用 - 开头列出，每条尽量包含：场景名称/入口/角色/关键校验点）。
2) 再基于清单生成测试用例：每个场景至少 2 条（1 条正向 + 1 条反向/异常）；复杂场景补充边界/权限/并发或重复提交/数据一致性，使该场景达到 3-5 条。
3) 总用例数由场景数量与复杂度自然决定，不要刻意收敛到固定数字。
4) 测试用例编号从 TC-001 开始连续递增，不设编号/数量上限。"""
        if file_extension in ['png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp']:
            # 处理图像文件
            if AGImage is None or AGMultiModalMessage is None:
                raise RuntimeError("缺少依赖 autogen-agentchat/autogen-core：请安装后再启用 AI 多模态功能")
            pil_image = PILImage.open(file_path)
            img = AGImage(pil_image)

            prompt = f"""请基于上传的图像生成测试用例（不要啰嗦，不要重复）。

上下文信息: {context}

需求: {requirements}

{scenario_driven_quantity}

**重要格式要求**：
请严格按照以下格式生成测试用例，这对于系统解析非常重要：

1. 每个测试用例必须以二级标题开始：## TC-001: 测试标题
2. 每个测试用例必须包含以下字段（使用加粗格式）：
   - **优先级:** 高/中/低
   - **描述:** 测试用例的详细描述
   - **前置条件:** 执行测试前的条件（如果有）

3. 测试步骤必须使用标准Markdown表格格式：

### 测试步骤

| # | 步骤描述 | 预期结果 |
| --- | --- | --- |
| 1 | 具体的操作步骤 | 期望看到的结果 |
| 2 | 下一个操作步骤 | 对应的期望结果 |

**示例格式**：
## TC-001: 用户登录功能测试

**优先级:** 高

**描述:** 验证用户能够使用正确的用户名和密码成功登录系统

**前置条件:** 用户账户已存在且处于激活状态

### 测试步骤

| # | 步骤描述 | 预期结果 |
| --- | --- | --- |
| 1 | 打开登录页面 | 显示登录表单 |
| 2 | 输入有效用户名和密码 | 输入框显示内容 |
| 3 | 点击登录按钮 | 成功登录并跳转到主页 |

质量要求：
1) 严格去重：同义/同目的用例不要重复；能合并则合并；能用“数据组合/参数化”表达则不要拆成多条。
2) 控制篇幅：**描述**最多一行；每条用例步骤建议 3-6 步；**前置条件**无则写“无”。
3) 覆盖维度：正向、负向、边界、权限/角色、异常提示与可用性（图中若有表单/列表/按钮/弹窗都要覆盖）。"""

            # 创建多模态消息（图像+文本）
            multi_modal_message = AGMultiModalMessage(content=[prompt, img], source="user")
            task_message = multi_modal_message

        elif file_extension == 'pdf':
            try:
                from ai_assistant.services.pdf_service import pdf_service
                task_message = "__pdf__"
            except Exception as e:
                yield f"\n\n**错误**: PDF处理失败 - {str(e)}\n"
                return

        elif file_extension in ['json', 'yaml', 'yml']:
            # 处理OpenAPI文件
            try:
                from ai_assistant.services.openapi_service import openapi_service
                # 解析OpenAPI文档
                api_data = openapi_service.parse_openapi_file(file_path)
                api_info = api_data['api_info']
                scenarios = openapi_service.generate_test_scenarios(api_info)
                operations_count = 0
                for p in api_info.get("paths") or []:
                    operations_count += len((p or {}).get("operations") or [])
                estimated_cases = max(20, operations_count * 4, len(scenarios or []))
                if estimated_cases > 200:
                    estimated_cases = 200

                # 构建API分析提示词
                prompt = f"""请基于上传的OpenAPI/Swagger文档生成API测试用例（不要啰嗦，不要重复）。

API文档信息:
- 标题: {api_info['info'].get('title', '未知')}
- 版本: {api_info['info'].get('version', '未知')}
- 描述: {api_info['info'].get('description', '无描述')}
- API路径数量: {len(api_info['paths'])}

API端点概览:
{self._format_api_endpoints_for_prompt(api_info)}

测试场景概览（用于覆盖完整性）:
{self._format_test_scenarios_for_prompt(scenarios)}

上下文信息: {context}

需求: {requirements}

数量要求（按场景动态生成，不固定 20 条）：
1) 先对每个端点识别“功能场景/鉴权场景/参数校验场景/错误码场景/边界场景”等覆盖点，并列出《场景清单》（用 - 开头）。
2) 再基于清单生成测试用例：每个端点至少包含 1 条正向 + 1 条鉴权失败/权限不足 + 1 条参数校验失败；对关键端点补充边界/幂等/并发/重复提交/数据一致性等。
3) 总数量由端点规模与场景复杂度决定，建议总量通常约 {estimated_cases} 条左右（允许合理浮动），不要把总数限制在 20 条以内。

请先以 Markdown 格式生成测试用例，包含以下内容：
1. 测试用例 ID 和标题（使用二级标题格式，如 ## TC-001: 测试标题）
2. 优先级（加粗显示，如 **优先级:** 高）
3. 描述（加粗显示，如 **描述:** 测试描述）
4. 前置条件（如果有，加粗显示，如 **前置条件:** 条件描述）
5. 测试步骤和预期结果（使用标准 Markdown 表格格式）

对于测试步骤表格，请使用以下格式：

```
### 测试步骤

| # | 步骤描述 | 预期结果 |
| --- | --- | --- |
| 1 | 第一步描述 | 第一步预期结果 |
| 2 | 第二步描述 | 第二步预期结果 |
```

请确保表格格式正确，包含表头和分隔行。

质量要求：
1) 严格去重：同一端点同目的用例不要重复；能参数化（不同参数组/不同返回码）可合并在一条用例说明里，但步骤要清晰。
2) 覆盖优先：鉴权/权限、参数校验、边界、幂等/重复提交、并发、异常与错误码。
3) 控制篇幅：描述最多一行；步骤 3-6 步；预期结果写清响应码/关键字段/错误信息。"""

                # 创建文本消息
                task_message = prompt
            except Exception as e:
                yield f"\n\n**错误**: OpenAPI文档处理失败 - {str(e)}\n"
                return

        else:
            # 处理其他文本文件
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    file_content = f.read()
            except UnicodeDecodeError:
                try:
                    with open(file_path, 'r', encoding='gbk') as f:
                        file_content = f.read()
                except:
                    yield f"\n\n**错误**: 无法读取文件内容，不支持的编码格式\n"
                    return

            from ai_assistant.services.testcase_pipeline import TestCasePipeline
            pipeline = TestCasePipeline()
            title = os.path.basename(file_path)
            async for chunk in pipeline.generate_from_text_stream(title, file_content, context, requirements, cancel_event=cancel_event):
                yield chunk
            return

            prompt = f"""请基于上传的文件内容生成测试用例（不要啰嗦，不要重复）。

文件内容:
{file_content[:5000]}{'...(内容过长，已截断)' if len(file_content) > 5000 else ''}

上下文信息: {context}

需求: {requirements}

{scenario_driven_quantity}

请先以 Markdown 格式生成测试用例，包含以下内容：
1. 测试用例 ID 和标题（使用二级标题格式，如 ## TC-001: 测试标题）
2. 优先级（加粗显示，如 **优先级:** 高）
3. 描述（加粗显示，如 **描述:** 测试描述）
4. 前置条件（如果有，加粗显示，如 **前置条件:** 条件描述）
5. 测试步骤和预期结果（使用标准 Markdown 表格格式）

质量要求：
1) 严格去重；能合并则合并；能参数化则不要拆成多条。
2) 控制篇幅：描述最多一行；步骤 3-6 步；前置条件无则写“无”。
3) 覆盖维度：正向/负向、边界、异常提示、权限/角色、数据一致性（如有）。"""

            # 创建文本消息
            task_message = prompt

        # 创建AI代理
        agent = self._create_agent(selected_model_client, system_message)

        # 首先输出标题
        yield "# 正在生成测试用例...\n\n"
        yield f"**文件信息**\n"
        yield f"- 文件类型: {file_extension.upper() if file_extension else '未知'}\n"
        yield "- 使用模型: 自动选择（文本/视觉）\n\n"
        yield "- 数量策略: 按功能场景动态生成（不固定 20 条）\n\n"
        yield "---\n\n"

        if file_extension == "pdf":
            from ai_assistant.services.pdf_service import pdf_service

            text_data = await asyncio.to_thread(pdf_service.extract_text_from_pdf, file_path)
            text_material = (text_data.get("text") or "").strip()
            metadata = text_data.get("metadata") or {}
            pages_total = int(metadata.get("pages") or 0) if isinstance(metadata, dict) else 0

            pages_with_images = await asyncio.to_thread(pdf_service.list_pages_with_images, file_path, 200)
            pages_with_images = [p for p in (pages_with_images or []) if isinstance(p, dict)]
            pages_with_images.sort(key=lambda x: int(x.get("images") or 0), reverse=True)
            max_visual_pages = 12
            selected_pages = pages_with_images[:max_visual_pages]

            yield "**PDF解析**\n"
            yield f"- 页数: {pages_total or '未知'}\n"
            yield f"- 含图页数: {len(pages_with_images)}\n"
            yield f"- 送视觉理解页数: {len(selected_pages)}\n\n"

            if not text_material and not selected_pages:
                yield "\n\n**错误**: PDF 未抽取到文本且未检测到图片页\n"
                return

            image_insights: List[Dict[str, Any]] = []
            if selected_pages:
                if AGImage is None or AGMultiModalMessage is None:
                    yield "\n\n**错误**: 缺少依赖 autogen-agentchat/autogen-core，无法进行多模态图片理解\n"
                    return
                vision_system = "你是视觉需求分析助手。只能基于图片可见内容输出，不允许臆测补全。"
                vision_agent = self._create_agent(get_vision_model_client(), vision_system)
                for p in selected_pages:
                    page_index = int(p.get("page_index") or 0)
                    page_no = int(p.get("page") or (page_index + 1))
                    pil_img = await asyncio.to_thread(pdf_service.render_page_image, file_path, page_index, 2.0)
                    img = AGImage(pil_img)
                    prompt = f"""请理解这张PDF页面截图的内容，并输出 JSON 对象（不要额外文字，不要使用```包裹）。

结构：
{{
  "page": {page_no},
  "image_type": "页面截图|流程图|原型图|架构图|表格截图|其他",
  "summary": "一句话概述页面/图的含义",
  "key_elements": ["可见的关键元素/字段/按钮/节点/状态等"],
  "implied_requirements": ["从可见内容可以直接得出的需求点（只写确定的）"],
  "uncertainties": ["无法确认或看不清的点，必须写出来"]
}}

规则：
1) 只写你在图上明确看到的内容；不允许补全未出现的字段/流程。
2) 看不清就写到 uncertainties，不要瞎猜。"""
                    mm = AGMultiModalMessage(content=[prompt, img], source="user")
                    raw = await self._run_agent_to_text(vision_agent, mm)
                    obj = self._extract_json_object_from_text(raw)
                    if not obj:
                        raw = await self._run_agent_to_text(vision_agent, "只输出一个 JSON 对象，不要额外文字。")
                        obj = self._extract_json_object_from_text(raw)
                    if obj:
                        obj["page"] = page_no
                        image_insights.append(obj)
                        yield f"- 已理解图片页 P{page_no}\n"
                yield "\n"

            analysis_system = "你是需求分析与测试专家。只能基于材料输出；证据不足的需求点必须标记为不确定并避免生成用例。"
            analysis_agent = self._create_agent(get_text_model_client(), analysis_system)
            page_blocks: List[Dict[str, Any]] = []
            tokens = re.split(r"\r?\n--- 第 (\\d+) 页 ---\r?\n", text_material or "")
            if len(tokens) >= 3:
                i = 1
                while i + 1 < len(tokens):
                    try:
                        page_no = int(str(tokens[i]).strip())
                    except Exception:
                        page_no = 0
                    content = str(tokens[i + 1] or "").strip()
                    if content:
                        page_blocks.append({"page": page_no, "text": content})
                    i += 2
            else:
                if (text_material or "").strip():
                    page_blocks.append({"page": 0, "text": (text_material or "").strip()})

            chunks: List[Dict[str, Any]] = []
            buf_pages: List[int] = []
            buf_texts: List[str] = []
            buf_len = 0
            for pb in page_blocks:
                t = str(pb.get("text") or "").strip()
                if not t:
                    continue
                pno = int(pb.get("page") or 0)
                addition = (f"\n【P{pno}】\n" if pno else "\n") + t
                if buf_len > 0 and buf_len + len(addition) > 6500:
                    chunks.append({"pages": buf_pages[:], "text": "\n".join(buf_texts).strip()})
                    buf_pages, buf_texts, buf_len = [], [], 0
                buf_pages.append(pno)
                buf_texts.append(addition)
                buf_len += len(addition)
            if buf_texts:
                chunks.append({"pages": buf_pages[:], "text": "\n".join(buf_texts).strip()})
            if not chunks:
                yield "\n\n**错误**: PDF 文本抽取为空，无法拆分功能点\n"
                return

            yield f"**功能点拆分**\n- 文本分块: {len(chunks)} 块\n\n"

            func_points: List[Dict[str, Any]] = []
            for idx, ch in enumerate(chunks, 1):
                pages_in_chunk = [p for p in (ch.get("pages") or []) if isinstance(p, int) and p > 0]
                min_p = min(pages_in_chunk) if pages_in_chunk else 0
                max_p = max(pages_in_chunk) if pages_in_chunk else 0
                related_images = []
                if min_p and max_p:
                    for it in image_insights:
                        if isinstance(it, dict) and int(it.get("page") or 0) >= min_p and int(it.get("page") or 0) <= max_p:
                            related_images.append(it)
                else:
                    related_images = image_insights[:]

                fp_prompt = f"""请仅基于本分块材料拆分“需求功能点清单”，输出 JSON 数组（不要额外文字，不要使用```包裹）。

每个功能点结构：
{{
  "fid": "F01",
  "title": "功能点标题",
  "description": "一句话说明",
  "complexity": "simple|medium|complex",
  "evidence_text": [{{"quote": "来自本分块文本原文短句（20-160字）"}}],
  "evidence_images": [{{"page": 1, "evidence": "来自图片理解的可见证据描述"}}]
}}

硬规则：
1) evidence_text.quote 必须是本分块“原文片段”，不要改写，不要总结替代原文。
2) evidence_images 只能引用下方【分块相关图片理解】中的明确内容，并给出页码。
3) 如果某功能点缺少任何证据（两类都为空），就不要输出该功能点。
4) 不要为了凑整/凑数量强行拆分重复功能点；也不要把所有功能点合并成少数几条，要尽量细化到可测试的功能点粒度。
5) 禁止输出“示例/占位词/模板字样”（如“功能点标题示例”“示例功能点”）；title 必须是具体可测的业务动作（例如“创建项目”“删除项目（仅管理员）”）。

【分块文本（仅可引用这里的原文作为证据）】
{ch.get("text")}

【分块相关图片理解（可为空）】
{self._clip_text(json.dumps(related_images, ensure_ascii=False), 12000)}
"""
                fp_text = await self._run_agent_to_text(analysis_agent, fp_prompt)
                pts = self._extract_json_array_from_text(fp_text)
                if not pts:
                    retry = "你刚才没有输出有效 JSON 数组。请只输出 JSON 数组，且必须包含 evidence_text/evidence_images。"
                    fp_text = await self._run_agent_to_text(analysis_agent, retry + "\n\n" + fp_prompt)
                    pts = self._extract_json_array_from_text(fp_text)
                if pts:
                    func_points.extend(pts)
                yield f"- 分块 {idx}/{len(chunks)}: pages {min_p}-{max_p} 提取到 {len(pts)} 个功能点\n"
            yield "\n"

            if not func_points:
                yield "\n\n**错误**: 未能从材料中拆分出功能点清单\n"
                return

            valid_points: List[Dict[str, Any]] = []
            for fp in func_points:
                ev_text = fp.get("evidence_text") or []
                ev_img = fp.get("evidence_images") or []
                ev_text_ok: List[Dict[str, Any]] = []
                if isinstance(ev_text, list):
                    for it in ev_text[:3]:
                        if not isinstance(it, dict):
                            continue
                        q = (it.get("quote") or "").strip()
                        if q and self._quote_in_text(q, text_material):
                            ev_text_ok.append({"quote": q})
                ev_img_ok: List[Dict[str, Any]] = []
                if isinstance(ev_img, list):
                    for it in ev_img[:3]:
                        if not isinstance(it, dict):
                            continue
                        pg = it.get("page")
                        ev = (it.get("evidence") or "").strip()
                        if pg and ev:
                            ev_img_ok.append({"page": int(pg), "evidence": ev})
                if not ev_text_ok and not ev_img_ok:
                    continue
                fp["evidence_text"] = ev_text_ok
                fp["evidence_images"] = ev_img_ok
                valid_points.append(fp)
            seen = set()
            dedup: List[Dict[str, Any]] = []
            for fp in valid_points:
                key = re.sub(r"\\s+", " ", str(fp.get("title") or "").strip().lower())
                if not key:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                dedup.append(fp)
            func_points = dedup

            yield "## 需求功能点清单\n\n"
            for fp in func_points[:120]:
                fid = (fp.get("fid") or "").strip()
                title = (fp.get("title") or "").strip()
                if title:
                    yield f"- {fid + ' ' if fid else ''}{title}\n"
            total_min = 0
            total_max = 0
            for fp in func_points:
                c = str(fp.get("complexity") or "medium").strip().lower()
                if c == "simple":
                    mn, mx = 3, 6
                elif c == "complex":
                    mn, mx = 6, 12
                else:
                    mn, mx = 4, 8
                total_min += mn
                total_max += mx
            yield f"\n**统计**: 功能点 {len(func_points)} 个；建议用例总数约 {total_min}–{total_max} 条（按覆盖需要可更多）。\n"
            yield "\n---\n\n"

            batch_size = 5
            tc_start = 1
            for bi in range(0, len(func_points), batch_size):
                batch = func_points[bi : bi + batch_size]
                if not batch:
                    continue
                batch_min = 0
                batch_max = 0
                for fp in batch:
                    c = str(fp.get("complexity") or "medium").strip().lower()
                    if c == "simple":
                        mn, mx = 3, 6
                    elif c == "complex":
                        mn, mx = 6, 12
                    else:
                        mn, mx = 4, 8
                    batch_min += mn
                    batch_max += mx
                evidence_lines: List[str] = []
                for fp in batch:
                    for it in (fp.get("evidence_text") or [])[:3]:
                        if isinstance(it, dict) and it.get("quote"):
                            evidence_lines.append(f'TEXT: "{it.get("quote")}"')
                    for it in (fp.get("evidence_images") or [])[:3]:
                        if isinstance(it, dict) and it.get("page") and it.get("evidence"):
                            evidence_lines.append(f'IMG P{it.get("page")}: {it.get("evidence")}')
                evidence_block = self._clip_text("\n".join(evidence_lines), 12000)

                batch_agent = self._create_agent(get_text_model_client(), system_message)
                batch_prompt = f"""请基于“功能点 JSON + 证据”分批生成测试用例。

强约束（防止瞎编）：
1) 每条测试用例必须包含 **依据:** 字段，引用 1-3 条证据（从下方证据块原样摘取），格式示例：**依据:** TEXT \"...\"；IMG P3 ...。
2) 不允许新增证据中不存在的字段/页面/按钮/接口/流程；证据不足就不要生成该点的用例。
3) 覆盖策略：每个功能点至少 3 条用例（正向 + 反向/异常 + 边界或权限）；complexity=complex 的功能点建议 5-8 条。功能点很少时，不要为了“凑整/凑数量”强行拆出大量重复用例。
4) 用例编号从本批起始编号连续递增，本批起始为 TC-{tc_start:03d}；不要重置为 TC-001；不要把总数量固定在 20 或其他整数上限。
5) 本批建议生成约 {batch_min}–{batch_max} 条用例（按覆盖需要可更多，但不要超过 {batch_max} 的两倍）。如果因为证据不足无法达到下限，请在最后输出“未生成原因摘要”。

输出格式要求：
每个用例标题行必须严格使用二级标题，且不要加粗，不要用 ###/####：
## TC-001: 测试标题
**优先级:** 高/中/低
**描述:** 一行
**前置条件:** 无/...
**依据:** TEXT \"原文\"；IMG Pn ...
### 测试步骤
| # | 步骤描述 | 预期结果 |
| --- | --- | --- |
| 1 | ... | ... |

功能点 JSON：
{json.dumps(batch, ensure_ascii=False)}

证据块（仅可引用这些，不可超出）：
{evidence_block}

上下文信息（可能为空）：
{context}

补充需求（可能为空）：
{requirements}
"""
                yield f"## 用例批次 {bi // batch_size + 1}（建议 {batch_min}–{batch_max} 条）\n\n"
                buf: List[str] = []
                async for chunk in self._stream_agent(batch_agent, batch_prompt, cancel_event=cancel_event):
                    if cancel_event is not None and cancel_event.is_set():
                        yield "\n\n**已停止**\n"
                        return
                    buf.append(chunk)
                    yield chunk
                produced = len(re.findall(r"^##\\s*TC-", "".join(buf), flags=re.MULTILINE | re.IGNORECASE))
                last_no = self._find_last_tc_no("".join(buf))
                continue_tries = 0
                while produced < batch_min and last_no and continue_tries < 2:
                    continue_tries += 1
                    cont_start = last_no + 1
                    cont_prompt = f"""继续补充生成测试用例，从 TC-{cont_start:03d} 开始连续编号。

约束与输出格式同上一批次；仍然必须包含 **依据:**，且依据只能从下方证据块原样引用；不要重复已有用例；不要为了凑整/凑数量而重复。

目标：把本批用例数量补足到至少 {batch_min} 条（当前约 {produced} 条）。

功能点 JSON：
{json.dumps(batch, ensure_ascii=False)}

证据块（仅可引用这些，不可超出）：
{evidence_block}
"""
                    more_buf: List[str] = []
                    async for chunk in self._stream_agent(batch_agent, cont_prompt, cancel_event=cancel_event):
                        if cancel_event is not None and cancel_event.is_set():
                            yield "\n\n**已停止**\n"
                            return
                        more_buf.append(chunk)
                        yield chunk
                    buf.extend(more_buf)
                    produced = len(re.findall(r"^##\\s*TC-", "".join(buf), flags=re.MULTILINE | re.IGNORECASE))
                    last_no = self._find_last_tc_no("".join(buf))
                yield "\n\n---\n\n"
                tc_start = (last_no + 1) if last_no else (tc_start + len(batch) * 4)
            return

        async for chunk in self._stream_agent(agent, task_message, cancel_event=cancel_event):
            yield chunk

    def _format_api_endpoints_for_prompt(self, api_info: Dict[str, Any]) -> str:
        """
        格式化API端点信息用于提示词
        """
        formatted = ""

        for path_info in api_info['paths'][:30]:
            formatted += f"### {path_info['path']}\n"
            for op in path_info['operations']:
                formatted += f"- **{op['method']}**: {op['summary'] or op['description']}\n"
                if op['parameters']:
                    formatted += f"  - 参数: {len(op['parameters'])} 个\n"
                if op['responses']:
                    formatted += f"  - 响应: {', '.join(op['responses'].keys())}\n"
            formatted += "\n"

        return formatted

    def _format_test_scenarios_for_prompt(self, test_scenarios: List[Dict[str, Any]]) -> str:
        formatted = ""
        if not test_scenarios:
            return formatted
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for s in test_scenarios:
            t = (s.get("scenario_type") or "other").strip()
            buckets.setdefault(t, []).append(s)
        for t, items in buckets.items():
            formatted += f"- {t}: {len(items)} 条\n"
            for it in items[:4]:
                title = (it.get("test_case_title") or "").strip()
                if title:
                    formatted += f"  - {title}\n"
        return formatted.strip() + "\n"

ai_service = AIService()
