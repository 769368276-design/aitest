from __future__ import annotations

import asyncio
import re
import threading
from typing import AsyncGenerator, List, Tuple
from contextlib import nullcontext

from django.conf import settings

from ai_assistant.utils.llms import get_text_model_client, get_vision_model_client
from ai_assistant.services.pdf_service import pdf_service
from ai_assistant.services.testcase_generation.types import StreamEvent
from ai_assistant.services.testcase_generation.agents import AGImage, AGMultiModalMessage, create_agent, run_agent_to_text, stream_agent
from ai_assistant.services.testcase_generation.postprocess import ensure_markdown_parseable, parse_cases_from_markdown, dedup_cases, cases_to_markdown


def _clip(text: str, max_len: int) -> str:
    t = (text or "").strip()
    if len(t) <= max_len:
        return t
    return t[:max_len] + "\n\n(已截断)"


def _page_targets(images_cnt: int, signal: int) -> Tuple[int, int, int]:
    images_cnt = int(images_cnt or 0)
    signal = int(signal or 0)
    if images_cnt > 0 and signal >= 160:
        return 12, 20, 10
    if images_cnt > 0 and signal >= 60:
        return 10, 18, 8
    if images_cnt > 0:
        return 8, 15, 6
    if signal >= 200:
        return 8, 14, 6
    if signal >= 80:
        return 6, 10, 5
    return 2, 6, 3


def _downscale_image(pil_img, max_dim: int):
    try:
        max_dim = int(max_dim or 0)
    except Exception:
        max_dim = 0
    if max_dim <= 0:
        return pil_img
    try:
        w, h = pil_img.size
    except Exception:
        return pil_img
    if w <= max_dim and h <= max_dim:
        return pil_img
    try:
        scale = max_dim / float(max(w, h))
        nw = max(1, int(w * scale))
        nh = max(1, int(h * scale))
        return pil_img.resize((nw, nh))
    except Exception:
        return pil_img


def _build_page_prompt(
    context: str,
    requirements: str,
    page_no: int,
    page_text: str,
    global_summary: str,
    tc_start: int,
    target_min: int,
    target_max: int,
    min_scenarios: int,
    recent_titles: List[str],
    only_new: bool = False,
) -> str:
    avoid = "\n".join([f"- {t}" for t in (recent_titles or [])[-60:]]).strip() or "- （无）"
    only_new_rule = "只输出新增的测试用例，不要重复已生成标题；不要输出任何解释文字。" if only_new else ""
    return f"""请基于上传的“PDF第 {page_no} 页截图”生成测试用例。

上下文信息: {context}

需求: {requirements}

约束：上下文/需求仅用于补充范围与重点，不要把它们当作“需求文档章节”来生成用例；不要为背景/目标/范围/术语/修订记录/目录/概述等章节本身生成用例，除非截图/该页文本抽取中明确出现对应可交互模块/页面/字段。

【全局摘要（用于术语对齐，可为空）】
{_clip(global_summary or '无', 2200)}

【该页文本抽取（用于术语对齐，可能为空）】
{_clip(page_text or '无', 5000)}

【已生成用例标题（避免同一目的重复；可参数化不要硬拆）】
{avoid}

数量要求（本页）：
- 场景要点至少 {int(min_scenarios)} 条（越细越好），然后输出测试用例。
- 测试用例目标输出 {int(target_min)}–{int(target_max)} 条（能多写则多写，但不要少于 {int(target_min)} 条）。
- 每个场景至少 1 正向 + 1 反向/异常；涉及输入/规则的，再补充边界/空值/长度/格式。
- 列表/分页/筛选/排序/导入导出/复制粘贴/撤销重做/权限/状态流转（截图里出现就必须覆盖）。
- 测试用例编号必须从 TC-{tc_start:03d} 开始连续递增。

输出规则：
{only_new_rule}

输出格式（必须严格遵循）：
### P{page_no} 场景要点
- ...

### P{page_no} 测试用例
## TC-001: 标题
**优先级:** 高/中/低
**描述:** 一行
**前置条件:** 无/...
### 测试步骤
| # | 步骤描述 | 预期结果 |
| --- | --- | --- |
| 1 | ... | ... |
"""


class PdfStrategy:
    async def generate(self, file_path: str, context: str, requirements: str, cancel_event: threading.Event | None = None, user=None) -> AsyncGenerator[StreamEvent, None]:
        mode = str(getattr(settings, "AI_PDF_PAGEWISE_MODE", "balanced") or "balanced").strip().lower()
        if mode not in ("fast", "balanced", "full"):
            mode = "balanced"
        max_pages = int(getattr(settings, "AI_PDF_PAGEWISE_MAX_PAGES", 80) or 80)
        enable_ocr = bool(getattr(settings, "AI_PDF_PAGEWISE_OCR", False)) and mode != "fast"
        yield StreamEvent(type="meta", message="pdf_start")
        yield StreamEvent(type="delta", text="# 正在解析 PDF...\n\n")

        pdf_extract_timeout = float(getattr(settings, "AI_PDF_EXTRACT_TIMEOUT_SECONDS", 180) or 180)
        try:
            structured = await asyncio.wait_for(
                pdf_service.extract_structured_from_pdf(
                    file_path,
                    enable_ocr=enable_ocr,
                    max_pages=max_pages,
                    max_ocr_pages=(8 if enable_ocr else 0),
                    user=user,
                ),
                timeout=pdf_extract_timeout if pdf_extract_timeout and pdf_extract_timeout > 0 else None,
            )
        except asyncio.TimeoutError:
            yield StreamEvent(type="delta", text="**错误**: PDF 解析超时，请降低 PDF 页数/关闭 OCR 后重试。\n")
            yield StreamEvent(type="done", message="pdf_timeout")
            return

        pages = structured.get("pages") or []
        yield StreamEvent(type="delta", text="# 正在生成测试用例...\n\n")

        global_summary = ""
        llm_material = structured.get("llm_material") or ""
        enable_global_summary = bool(getattr(settings, "AI_PDF_PAGEWISE_GLOBAL_SUMMARY", False)) and mode == "full"
        if enable_global_summary and llm_material.strip() and mode != "fast":
            sum_agent = create_agent(get_text_model_client(user=user), "你是需求摘要助手。输出紧凑摘要便于术语对齐。", "pdf_summary_agent")
            sum_prompt = f"""请基于以下PDF结构化文本，输出一份极短摘要（尽量控制在 20 行以内）。

输出结构：
### 关键术语/模块
- ...

### 关键流程/状态
- ...

### 关键约束/校验
- ...

【PDF结构化文本】
{_clip(llm_material, 45000)}
"""
            try:
                llm_timeout = float(getattr(settings, "AI_PDF_SUMMARY_TIMEOUT_SECONDS", 60) or 60)
                global_summary = (
                    await asyncio.wait_for(
                        run_agent_to_text(sum_agent, sum_prompt, cancel_event=cancel_event),
                        timeout=llm_timeout if llm_timeout and llm_timeout > 0 else None,
                    )
                ).strip()
            except Exception:
                global_summary = ""

        vision_agent = create_agent(get_vision_model_client(user=user), "你是专业的测试用例生成器。输出严格可解析的Markdown用例。", "pdf_cases_agent")

        skip_low = bool(getattr(settings, "AI_PDF_PAGEWISE_SKIP_LOW_VALUE", True)) and mode != "full"
        render_scale = float(getattr(settings, "AI_PDF_PAGEWISE_RENDER_SCALE", 1.4) or 1.4)
        max_dim = int(getattr(settings, "AI_PDF_PAGEWISE_IMAGE_MAX_DIM", 1400) or 1400)
        if mode == "fast":
            render_scale = min(render_scale, 1.2)
            max_dim = min(max_dim, 1200)

        tc_start = 1
        recent_titles: List[str] = []
        recent_title_keys = set()
        normalized_parts: List[str] = []

        for p in pages:
            if cancel_event is not None and cancel_event.is_set():
                yield StreamEvent(type="delta", text="\n\n**已停止**\n")
                break
            page_no = int((p or {}).get("page") or 0)
            images_cnt = int((p or {}).get("images") or 0)
            signal = int((p or {}).get("signal") or 0)
            if skip_low and images_cnt <= 0 and signal < 80:
                continue

            page_text = (
                str((p or {}).get("text") or "").strip()
                + "\n"
                + str((p or {}).get("tables") or "").strip()
                + "\n"
                + str((p or {}).get("ocr_text") or "").strip()
            ).strip()

            target_min, target_max, min_scen = _page_targets(images_cnt, signal)
            yield StreamEvent(type="delta", text=f"\n\n## 第 P{page_no} 页\n\n")

            try:
                pil_img = await asyncio.to_thread(pdf_service.render_page_image, file_path, max(page_no - 1, 0), render_scale)
            except Exception as e:
                yield StreamEvent(type="delta", text=f"**错误**: 第 P{page_no} 页渲染失败: {str(e)}\n")
                continue
            pil_img = _downscale_image(pil_img, max_dim)

            prompt = _build_page_prompt(
                context=context or "",
                requirements=requirements or "",
                page_no=page_no,
                page_text=page_text,
                global_summary=global_summary,
                tc_start=tc_start,
                target_min=target_min,
                target_max=target_max,
                min_scenarios=min_scen,
                recent_titles=recent_titles,
                only_new=False,
            )
            mm = AGMultiModalMessage(content=[prompt, AGImage(pil_img)], source="user")
            page_parts: List[str] = []
            llm_timeout = float(getattr(settings, "AI_PDF_PAGE_TIMEOUT_SECONDS", 120) or 120)
            try:
                timeout_ctx = asyncio.timeout(llm_timeout) if llm_timeout and llm_timeout > 0 else nullcontext()
                async with timeout_ctx:
                    async for chunk in stream_agent(vision_agent, mm, cancel_event=cancel_event):
                        if not chunk:
                            continue
                        page_parts.append(chunk)
                        yield StreamEvent(type="delta", text=chunk)
            except TimeoutError:
                yield StreamEvent(type="delta", text=f"**错误**: 第 P{page_no} 页生成超时，已跳过该页。\n")
                continue

            raw_page = "".join(page_parts)
            fixed, last_no = ensure_markdown_parseable(raw_page, tc_start=tc_start)
            normalized_page = fixed or raw_page
            normalized_parts.append(normalized_page)
            tc_start = last_no + 1 if last_no >= tc_start else tc_start
            page_cases = parse_cases_from_markdown(normalized_page)

            for c in page_cases:
                t = str(c.get("title") or "").split(":", 1)[-1].strip()
                k = t.lower()
                k = re.sub(r"\s+", " ", k)
                if not k or k in recent_title_keys:
                    continue
                recent_title_keys.add(k)
                recent_titles.append(t)
                if len(recent_titles) > 60:
                    drop = recent_titles.pop(0)
                    dk = drop.lower()
                    dk = re.sub(r"\s+", " ", dk)
                    recent_title_keys.discard(dk)

            if mode == "full" and tc_start <= last_no:
                tc_start = last_no + 1

        merged = "\n".join([p for p in normalized_parts if str(p or "").strip()]).strip()
        cases = parse_cases_from_markdown(merged)
        cases = dedup_cases(cases)
        final_md, _ = cases_to_markdown(cases, tc_start=1) if cases else ensure_markdown_parseable(merged, tc_start=1)
        yield StreamEvent(type="final", text=final_md or merged)
        yield StreamEvent(type="done", message="pdf_done")
