import asyncio
import json
import re
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from django.conf import settings

from ai_assistant.services.pdf_service import pdf_service
from ai_assistant.services.testcase_postprocess import (
    fix_incomplete_last_case,
    normalize_case_headings,
    sort_case_blocks,
)
from ai_assistant.utils.llms import (
    get_review_model_client,
    get_text_model_client,
    get_vision_model_client,
)

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


def _extract_json_array(text: str) -> List[Dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return []
    if "BEGIN_JSON" in raw and "END_JSON" in raw:
        try:
            raw = raw.split("BEGIN_JSON", 1)[1].split("END_JSON", 1)[0]
        except Exception:
            pass
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9_-]*\r?\n", "", raw)
        raw = re.sub(r"\r?\n```$", "", raw.strip())
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


def _extract_json_object(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    if "BEGIN_JSON" in raw and "END_JSON" in raw:
        try:
            raw = raw.split("BEGIN_JSON", 1)[1].split("END_JSON", 1)[0]
        except Exception:
            pass
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9_-]*\r?\n", "", raw)
        raw = re.sub(r"\r?\n```$", "", raw.strip())
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


def _extract_points_from_bullets(text: str) -> List[Dict[str, Any]]:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    titles: List[str] = []
    for ln in lines:
        m = re.match(r"^[-*]\s+(.+)$", ln)
        if not m:
            m = re.match(r"^\d{1,2}[.)]\s+(.+)$", ln)
        if m:
            t = m.group(1).strip().strip("：:").strip()
            if t:
                titles.append(t)
    if not titles and lines:
        titles = lines[:10]
    out: List[Dict[str, Any]] = []
    for i, t in enumerate(titles[:60], 1):
        out.append(
            {
                "fid": f"F{i:02d}",
                "title": t[:120],
                "description": "",
                "complexity": "medium",
                "evidence": [{"type": "text", "page": 0, "quote": t[:160]}],
                "uncertainties": [],
            }
        )
    return out


def _clip(s: str, max_len: int) -> str:
    t = (s or "").strip()
    if len(t) <= max_len:
        return t
    return t[:max_len] + "\n...(内容过长，已截断)"


def _norm_key(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _signal_count(s: str) -> int:
    txt = re.sub(r"\s+", "", s or "")
    hits = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", txt)
    return len(hits)


def _find_last_tc_no(text: str) -> int:
    hits = re.findall(r"\bTC-(\d{1,6})\b", text or "", flags=re.IGNORECASE)
    if not hits:
        return 0
    try:
        return max(int(x) for x in hits)
    except Exception:
        return 0


def _ensure_deps() -> None:
    if AssistantAgent is None or ModelClientStreamingChunkEvent is None or TaskResult is None:
        raise RuntimeError("缺少依赖 autogen-agentchat/autogen-core：请安装后再启用 AI 功能")


class _LineTransformer:
    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: str) -> List[str]:
        if not chunk:
            return []
        self._buf += chunk
        outs: List[str] = []
        while True:
            if "\n" not in self._buf:
                break
            line, rest = self._buf.split("\n", 1)
            outs.append(self._transform_line(line) + "\n")
            self._buf = rest
        return outs

    def flush(self) -> str:
        if not self._buf:
            return ""
        out = self._transform_line(self._buf)
        self._buf = ""
        return out

    def _transform_line(self, line: str) -> str:
        return line


class _HeadingPrefixTransformer(_LineTransformer):
    _pat = re.compile(r"^(#{1,6}\s*)(\*{0,2})?(TC-\d{1,6})(\s*[:：].*)$", re.IGNORECASE)

    def __init__(self, prefix_word: str) -> None:
        super().__init__()
        self._prefix_word = (prefix_word or "").strip() or "DRAFT"

    def _transform_line(self, line: str) -> str:
        m = self._pat.match((line or "").strip())
        if not m:
            return line
        prefix = m.group(1)
        tc = m.group(3).upper()
        tail = m.group(4)
        return f"{prefix}{self._prefix_word} {tc}{tail}"


class _TcRenumberTransformer(_LineTransformer):
    _pat = re.compile(r"^(#{1,6}\s*)(\*{0,2})?(TC-\d{1,6})(\s*[:：].*)$", re.IGNORECASE)

    def __init__(self, tc_start: int) -> None:
        super().__init__()
        self._next_no = int(tc_start or 1)
        self.last_no = int(tc_start or 1) - 1
        self.titles: List[str] = []

    def _transform_line(self, line: str) -> str:
        raw = (line or "").rstrip()
        m = self._pat.match(raw.strip())
        if not m:
            return line
        prefix = m.group(1)
        tail = m.group(4) or ""
        title = re.sub(r"^\s*[:：]\s*", "", tail).strip()
        if title:
            self.titles.append(title)
        tc = f"TC-{self._next_no:03d}"
        self.last_no = self._next_no
        self._next_no += 1
        return f"{prefix}{tc}{tail}"


class _StreamCollector:
    def __init__(
        self,
        agent: AssistantAgent,
        task_message,
        transformer: Optional[_LineTransformer] = None,
        cancel_event=None,
    ) -> None:
        self._agent = agent
        self._task_message = task_message
        self._transformer = transformer
        self._cancel_event = cancel_event
        self._raw_parts: List[str] = []

    @property
    def text(self) -> str:
        return "".join(self._raw_parts)

    async def run(self) -> AsyncGenerator[str, None]:
        stream = self._agent.run_stream(task=self._task_message)
        try:
            async for event in stream:
                if self._cancel_event is not None and self._cancel_event.is_set():
                    try:
                        await stream.aclose()
                    except Exception:
                        pass
                    break
                if isinstance(event, ModelClientStreamingChunkEvent):
                    chunk = event.content or ""
                    self._raw_parts.append(chunk)
                    if self._transformer is None:
                        yield chunk
                    else:
                        for out in self._transformer.feed(chunk):
                            yield out
                elif isinstance(event, TaskResult):
                    break
        finally:
            try:
                await stream.aclose()
            except Exception:
                pass
        if self._transformer is not None:
            tail = self._transformer.flush()
            if tail:
                yield tail


class TestCasePipeline:
    def __init__(self, user=None) -> None:
        _ensure_deps()
        self.user = user

    def _agent(self, model_client, system_message: str, name: str) -> AssistantAgent:
        return AssistantAgent(
            name=name,
            model_client=model_client,
            system_message=system_message,
            model_client_stream=True,
        )

    async def _run_to_text(self, agent: AssistantAgent, task_message, cancel_event=None) -> str:
        collector = _StreamCollector(agent, task_message, cancel_event=cancel_event)
        async for _ in collector.run():
            pass
        return collector.text

    async def _build_pdf_digest(self, pdf_path: str, context: str, requirements: str) -> Dict[str, Any]:
        text_data = await asyncio.to_thread(pdf_service.extract_text_from_pdf, pdf_path)
        metadata = text_data.get("metadata") or {}
        extracted_text = (text_data.get("text") or "").strip()

        pages_with_images = await asyncio.to_thread(pdf_service.list_pages_with_images, pdf_path, 200)
        pages_with_images = [p for p in (pages_with_images or []) if isinstance(p, dict)]
        pages_with_images.sort(key=lambda x: int(x.get("images") or 0), reverse=True)

        max_visual_pages = 10
        selected_pages = pages_with_images[:max_visual_pages]
        image_insights: List[Dict[str, Any]] = []

        if selected_pages:
            if AGImage is None or AGMultiModalMessage is None:
                raise RuntimeError("缺少依赖 autogen-agentchat/autogen-core：无法进行多模态图片理解")
            vision_system = "你是视觉需求分析助手。只能基于图片可见内容输出，不允许臆测补全。"
            vision_agent = self._agent(get_vision_model_client(user=self.user), vision_system, "pdf_vision_agent")
            for p in selected_pages:
                page_index = int(p.get("page_index") or 0)
                page_no = int(p.get("page") or (page_index + 1))
                pil_img = await asyncio.to_thread(pdf_service.render_page_image, pdf_path, page_index, 2.0)
                mm = AGMultiModalMessage(
                    content=[
                        (
                            "请理解该PDF页面图片内容，输出 JSON 对象（不要额外文字，不要使用```包裹）："
                            "{\"page\":1,\"image_type\":\"页面截图|流程图|原型图|架构图|表格截图|其他\","
                            "\"summary\":\"一句话概述\","
                            "\"key_elements\":[\"可见元素\"],"
                            "\"requirements\":[\"从可见内容可直接得出的需求点\"],"
                            "\"uncertainties\":[\"看不清/无法确认的点\"]}"
                            "规则：只写图上明确看见的，不要猜。"
                        ),
                        AGImage(pil_img),
                    ],
                    source="user",
                )
                raw = await self._run_to_text(vision_agent, mm)
                obj = _extract_json_object(raw)
                if not obj:
                    raw = await self._run_to_text(vision_agent, "只输出一个 JSON 对象，不要额外文字。")
                    obj = _extract_json_object(raw)
                if obj:
                    obj["page"] = page_no
                    image_insights.append(obj)

        return {
            "metadata": metadata,
            "text": extracted_text,
            "images": image_insights,
            "images_pages_total": len(pages_with_images),
            "images_pages_used": len(selected_pages),
            "context": context or "",
            "requirements": requirements or "",
        }

    def _choose_chunk_chars(self, extracted_text: str) -> int:
        total = len((extracted_text or "").strip())
        if total <= 12000:
            return 12000
        target_chunks = 4
        chunk = max(9000, min(18000, int(total / max(1, target_chunks))))
        return chunk

    def _split_text_into_chunks(self, extracted_text: str, chunk_chars: Optional[int] = None) -> List[Dict[str, Any]]:
        tokens = re.split(r"\r?\n--- 第 (\d+) 页 ---\r?\n", extracted_text or "")
        page_blocks: List[Dict[str, Any]] = []
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
            if (extracted_text or "").strip():
                page_blocks.append({"page": 0, "text": (extracted_text or "").strip()})

        chunk_chars = int(chunk_chars or self._choose_chunk_chars(extracted_text))

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
            if buf_len > 0 and buf_len + len(addition) > int(chunk_chars):
                chunks.append({"pages": buf_pages[:], "text": "\n".join(buf_texts).strip()})
                buf_pages, buf_texts, buf_len = [], [], 0
            buf_pages.append(pno)
            buf_texts.append(addition)
            buf_len += len(addition)
        if buf_texts:
            chunks.append({"pages": buf_pages[:], "text": "\n".join(buf_texts).strip()})
        if len(chunks) <= 1:
            return chunks

        min_signal = 220
        merged: List[Dict[str, Any]] = []
        for ch in chunks:
            txt = str(ch.get("text") or "")
            sig = _signal_count(txt)
            if sig < min_signal and merged:
                prev = merged[-1]
                prev["pages"] = (prev.get("pages") or []) + (ch.get("pages") or [])
                prev["text"] = (str(prev.get("text") or "").rstrip() + "\n" + txt.lstrip()).strip()
                continue
            merged.append(ch)

        if len(merged) >= 2:
            first_sig = _signal_count(str(merged[0].get("text") or ""))
            if first_sig < min_signal:
                nxt = merged[1]
                nxt["pages"] = (merged[0].get("pages") or []) + (nxt.get("pages") or [])
                nxt["text"] = (str(merged[0].get("text") or "").rstrip() + "\n" + str(nxt.get("text") or "").lstrip()).strip()
                merged = merged[1:]
        return merged

    async def _extract_function_points(self, digest: Dict[str, Any], cancel_event=None) -> Tuple[List[Dict[str, Any]], List[str]]:
        extracted_text = digest.get("text") or ""
        images = digest.get("images") or []
        context = digest.get("context") or ""
        requirements = digest.get("requirements") or ""

        chunks = self._split_text_into_chunks(extracted_text)
        if not chunks:
            return [], ["pdf_text_empty"]

        def _quote_in_source(quote: str, src: str) -> bool:
            q = (quote or "").strip()
            if not q:
                return False
            s = src or ""
            if q in s:
                return True
            qn = re.sub(r"\s+", "", q)
            sn = re.sub(r"\s+", "", s)
            if not qn or not sn:
                return False
            if qn in sn:
                return True
            if len(qn) >= 24 and qn[:24] in sn:
                return True
            if len(qn) >= 16 and qn[:16] in sn:
                return True
            return False

        system = (
            "你是需求分析与测试专家。任务是把材料拆成可测试的功能点清单。"
            "必须避免臆测；不确定的点要写进 uncertainties，而不是当成需求。"
        )
        agent = self._agent(get_text_model_client(user=self.user), system, "func_point_agent")

        all_points: List[Dict[str, Any]] = []
        diag: List[str] = [f"text_chunks={len(chunks)}"]
        for idx, ch in enumerate(chunks, 1):
            pages = [p for p in (ch.get("pages") or []) if isinstance(p, int) and p > 0]
            min_p = min(pages) if pages else 0
            max_p = max(pages) if pages else 0
            related_images = []
            if min_p and max_p:
                for it in images:
                    if isinstance(it, dict):
                        pno = int(it.get("page") or 0)
                        if min_p <= pno <= max_p:
                            related_images.append(it)

            prompt = f"""请仅基于以下材料拆分“需求功能点清单”。\n\n输出要求：\n- 只输出一个 JSON 数组，必须用 BEGIN_JSON 与 END_JSON 包裹；除此之外不要输出任何文字。\n- 不要使用```代码块。\n\nBEGIN_JSON\n[\n  {{\n    \"fid\": \"F01\",\n    \"title\": \"功能点标题\",\n    \"description\": \"一句话说明\",\n    \"complexity\": \"simple|medium|complex\",\n    \"evidence\": [{{\"type\":\"text|image\",\"page\":1,\"quote\":\"原文短句或图片可见证据描述\"}}],\n    \"uncertainties\": [\"不确定项\"]\n  }}\n]\nEND_JSON\n\n规则：\n1) 数量原则：按复杂度自动决定功能点数量，不设上限；不要为了凑整重复。\n2) 粒度：拆到可测试的业务动作/规则层级，禁止模板/占位词。\n3) 证据：每个功能点至少 1 条 evidence；text evidence 必须是原文短句（尽量原样摘录）。\n4) 不确定：看不清/材料缺失就放 uncertainties，不要当成需求。\n5) 约束：上下文/补充需求仅用于理解范围与术语对齐，不能当做“材料证据”。evidence.quote 必须来自【分块文本】或【分块相关图片理解】的可见内容；如果只在“补充需求/上下文”出现，请放入 uncertainties 或忽略。\n6) 不要把“需求文档章节本身”（如 背景/目标/范围/术语/修订记录/目录/概述）当作功能点输出，除非在【分块文本/图片】里明确出现对应的可交互模块/页面/字段。\n\n上下文（可能为空）：\n{context}\n\n补充需求（可能为空）：\n{requirements}\n\n【分块文本】\n{_clip(ch.get('text') or '', 12000)}\n\n【分块相关图片理解】\n{_clip(json.dumps(related_images, ensure_ascii=False), 12000)}\n"""
            if cancel_event is not None and cancel_event.is_set():
                break
            raw = await self._run_to_text(agent, prompt, cancel_event=cancel_event)
            pts = _extract_json_array(raw)
            if not pts:
                raw2 = await self._run_to_text(
                    agent,
                    "把刚才的输出转换为严格 JSON 数组，并且只输出 BEGIN_JSON 与 END_JSON 包裹的那部分。",
                    cancel_event=cancel_event,
                )
                pts = _extract_json_array(raw2)
                if not pts:
                    pts = _extract_points_from_bullets(raw or raw2)
            filtered: List[Dict[str, Any]] = []
            src_text = str(ch.get("text") or "")
            src_images = json.dumps(related_images, ensure_ascii=False)
            for fp in pts or []:
                ev = fp.get("evidence") or []
                if not isinstance(ev, list) or not ev:
                    continue
                ok = False
                for e in ev:
                    if not isinstance(e, dict):
                        continue
                    t = str(e.get("type") or "").strip().lower()
                    quote = str(e.get("quote") or "").strip()
                    if t == "text" and _quote_in_source(quote, src_text):
                        ok = True
                        break
                    if t == "image" and _quote_in_source(quote, src_images):
                        ok = True
                        break
                if ok:
                    filtered.append(fp)
            pts = filtered
            diag.append(f"chunk_{idx}_pages={min_p}-{max_p}_pts={len(pts)}")
            all_points.extend(pts)

        dedup: List[Dict[str, Any]] = []
        seen = set()
        for fp in all_points:
            title = str(fp.get("title") or "").strip()
            if not title:
                continue
            key = _norm_key(title)
            if not key or key in seen:
                continue
            ev = fp.get("evidence") or []
            if not isinstance(ev, list) or not ev:
                continue
            fp["title"] = title
            fp["description"] = str(fp.get("description") or "").strip()
            fp["complexity"] = str(fp.get("complexity") or "medium").strip().lower()
            fp["evidence"] = ev[:6]
            fp["uncertainties"] = fp.get("uncertainties") or []
            seen.add(key)
            dedup.append(fp)
        return dedup, diag

    def _dedup_points(self, all_points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        dedup: List[Dict[str, Any]] = []
        seen = set()
        for fp in all_points or []:
            if not isinstance(fp, dict):
                continue
            title = str(fp.get("title") or "").strip()
            if not title:
                continue
            key = _norm_key(title)
            if not key or key in seen:
                continue
            ev = fp.get("evidence") or []
            if not isinstance(ev, list) or not ev:
                continue
            fp["title"] = title
            fp["description"] = str(fp.get("description") or "").strip()
            fp["complexity"] = str(fp.get("complexity") or "medium").strip().lower()
            fp["evidence"] = ev[:6]
            fp["uncertainties"] = fp.get("uncertainties") or []
            seen.add(key)
            dedup.append(fp)
        return dedup

    def _batch_targets(self, points: List[Dict[str, Any]]) -> Tuple[int, int]:
        total_min = 0
        total_max = 0
        for fp in points:
            c = str(fp.get("complexity") or "medium").strip().lower()
            if c == "simple":
                mn, mx = 3, 6
            elif c == "complex":
                mn, mx = 6, 12
            else:
                mn, mx = 4, 8
            total_min += mn
            total_max += mx
        return total_min, total_max

    def _build_writer_prompt(self, digest: Dict[str, Any], batch: List[Dict[str, Any]], tc_start: int) -> Tuple[str, str]:
        material = _clip(digest.get("text") or "", 16000)
        images = _clip(json.dumps(digest.get("images") or [], ensure_ascii=False), 12000)
        bmin, bmax = self._batch_targets(batch)
        writer_system = "你是资深测试工程师。基于需求功能点生成高覆盖率测试用例，步骤可执行，覆盖异常与边界。"
        prompt = f"""请基于“功能点 + 材料摘要”生成测试用例。

数量原则：按复杂度自动决定数量，不设上限，应写尽写；不要把总数量固定在 20 等整数上限。
深度遍历：逐个功能点生成；每个功能点至少 1 正向 + 2-3 异常/边界；拒绝合并多个验证点。
编号：从 TC-{tc_start:03d} 开始连续递增，必须按顺序输出，不能跳号/重复/乱序。
输出必须完整：即使超过 40/60 条也必须完整输出。

本批建议生成约 {bmin}–{bmax} 条（按覆盖需要可更多，但不要超过 {bmax * 2}）。

格式要求：
每条用例必须以二级标题开头：
## TC-001: 标题
**优先级:** 高/中/低
**描述:** 一行
**前置条件:** 无/...
### 测试步骤
| # | 步骤描述 | 预期结果 |
| --- | --- | --- |
| 1 | ... | ... |

【功能点（本批）】
{_clip(json.dumps(batch, ensure_ascii=False), 12000)}

【材料摘要】
{material}

【图片理解摘要】
{images}
"""
        return writer_system, prompt

    def _build_review_prompt(self, digest: Dict[str, Any], batch: List[Dict[str, Any]], cases_markdown: str) -> Tuple[str, str]:
        material = _clip(digest.get("text") or "", 16000)
        images = _clip(json.dumps(digest.get("images") or [], ensure_ascii=False), 12000)
        reviewer_system = (
            "你是资深测试负责人，负责严格评审测试用例。"
            "重点：覆盖率漏洞、重复/冗余、预期是否可验证、疑似臆造（材料中找不到依据）。"
        )
        prompt = f"""请评审以下测试用例，并输出评审意见（不需要 JSON）。

【功能点（本批）】
{_clip(json.dumps(batch, ensure_ascii=False), 12000)}

【材料摘要】
{material}

【图片理解摘要】
{images}

【待评审用例】
{cases_markdown}

输出要求：
1) 用“问题列表”列出：覆盖缺口、重复、不可验证预期、疑似臆造（指出理由）。
2) 用“增补建议”列出应该补充的场景。
3) 用“删除建议”列出应该删除的用例编号。
4) 输出紧凑，段落之间不要空很多行。
"""
        return reviewer_system, prompt

    def _build_revise_prompt(
        self, digest: Dict[str, Any], batch: List[Dict[str, Any]], draft: str, review: str, tc_start: int
    ) -> Tuple[str, str]:
        material = _clip(digest.get("text") or "", 16000)
        images = _clip(json.dumps(digest.get("images") or [], ensure_ascii=False), 12000)
        revise_system = "你是资深测试工程师。根据需求与评审意见改进测试用例，保持格式规范与编号连续。"
        prompt = f"""请根据材料与评审意见，改进并输出本批测试用例的最终版本。

硬规则：
1) 只能基于材料与功能点，不允许臆造不存在的字段/页面/接口/流程。
2) 对每个功能点：至少 1 正向 + 2-3 异常/边界；拒绝把多个验证点合并在同一条用例里。
3) 编号要求：从 TC-{tc_start:03d} 开始连续递增，绝对不能跳号/重复/乱序。
4) 输出必须完整，不要因为篇幅省略第20/30/40条；也不要把数量收敛到固定值。
5) 输出只包含最终测试用例本身，不要包含任何说明文字。

格式要求：
每条用例必须以二级标题开头：
## TC-001: 标题
**优先级:** 高/中/低
**描述:** 一行
**前置条件:** 无/...
### 测试步骤
| # | 步骤描述 | 预期结果 |
| --- | --- | --- |
| 1 | ... | ... |

【功能点（本批）】
{_clip(json.dumps(batch, ensure_ascii=False), 12000)}

【材料摘要】
{material}

【图片理解摘要】
{images}

【原始用例（草稿）】
{draft}

【评审意见】
{review}
"""
        return revise_system, prompt

    def _build_screenshot_notes_prompt(self, context: str, requirements: str, page_no: int) -> str:
        return f"""请基于上传的截图提取“可测试信息要点”（不要生成测试用例，不要输出 JSON，不要出现 TC- 编号）。\n\n上下文信息: {context}\n\n需求: {requirements}\n\n输出要求（使用 Markdown 小标题 + 列表，内容要紧凑）：\n1) **页面/流程类型判断**：页面截图/原型图/流程图/表格/其他\n2) **可见要素清单**：按钮、输入框、字段名、表头、状态、弹窗、菜单等（只写看得见的）\n3) **可测场景清单**：按“正向/异常/边界/权限/状态流转”列出场景要点（仍然只基于截图可见内容，不要猜接口/字段规则）\n4) **不确定项**：看不清/无法确认的点\n\n请以 `### P{page_no} 图片要点` 作为第一行标题，然后输出上述内容。"""

    def _page_targets(self, images_cnt: int, signal: int) -> Tuple[int, int, int]:
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

    def _build_page_scenarios_prompt(
        self,
        context: str,
        requirements: str,
        page_no: int,
        page_text: str,
        global_summary: str,
        min_scenarios: int,
        recent_titles: List[str],
    ) -> str:
        avoid = "\n".join([f"- {t}" for t in (recent_titles or [])[-40:]]).strip() or "- （无）"
        return f"""请基于上传的“PDF第 {page_no} 页截图”提取《本页场景要点清单》（不要生成测试用例，不要出现 TC- 编号）。\n\n目标：输出尽可能细的、可测试的场景要点，至少 {int(min_scenarios)} 条。\n\n上下文信息: {context}\n\n需求: {requirements}\n\n【全局摘要（用于术语对齐，可为空）】\n{_clip(global_summary or '无', 2200)}\n\n【该页文本抽取（用于术语对齐，可能为空）】\n{_clip(page_text or '无', 5000)}\n\n【已生成用例标题（避免把同一目的重复当新场景）】\n{avoid}\n\n输出格式要求：\n- 只输出 Markdown 标题 + 列表，不要输出其他解释文字。\n- 第一行必须是：### P{page_no} 场景要点\n- 在“场景要点”里用 - 开头列出，每条尽量包含：入口/动作/校验点/异常提示/权限或状态。\n\n输出结构（必须包含）：\n### P{page_no} 场景要点\n- ...\n\n### P{page_no} 可见要素（可选，但尽量列）\n- ...\n"""

    def _downscale_image(self, pil_img, max_dim: int):
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

    def _build_page_combined_prompt(
        self,
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
        return f"""请基于上传的“PDF第 {page_no} 页截图”完成两件事：先输出本页《场景要点清单》，再输出本页《测试用例》。\n\n上下文信息: {context}\n\n需求: {requirements}\n\n【全局摘要（用于术语对齐，可为空）】\n{_clip(global_summary or '无', 2200)}\n\n【该页文本抽取（用于术语对齐，可能为空）】\n{_clip(page_text or '无', 5000)}\n\n【已生成用例标题（避免同一目的重复；可参数化不要硬拆）】\n{avoid}\n\n数量要求（本页）：\n- 场景要点至少 {int(min_scenarios)} 条，越细越好。\n- 测试用例目标输出 {int(target_min)}–{int(target_max)} 条（能多写则多写，但不要少于 {int(target_min)} 条）。\n- 每个场景至少 1 正向 + 1 反向/异常；涉及输入/规则的，再补充边界/空值/长度/格式。\n- 列表/分页/筛选/排序/导入导出/复制粘贴/撤销重做/权限/状态流转（截图里出现就必须覆盖）。\n- 测试用例编号必须从 TC-{tc_start:03d} 开始连续递增，跨页不重号、不跳号。\n\n输出规则：\n{only_new_rule}\n\n输出格式（必须严格遵循）：\n### P{page_no} 场景要点\n- ...\n\n### P{page_no} 测试用例\n## TC-001: 标题\n**优先级:** 高/中/低\n**描述:** 一行\n**前置条件:** 无/...\n### 测试步骤\n| # | 步骤描述 | 预期结果 |\n| --- | --- | --- |\n| 1 | ... | ... |\n"""

    def _build_page_continue_prompt(
        self,
        context: str,
        requirements: str,
        page_no: int,
        page_text: str,
        global_summary: str,
        tc_start: int,
        remaining_min: int,
        remaining_max: int,
        recent_titles: List[str],
    ) -> str:
        avoid = "\n".join([f"- {t}" for t in (recent_titles or [])[-80:]]).strip() or "- （无）"
        return f"""请继续基于上传的“PDF第 {page_no} 页截图”补充测试用例，只输出新增用例。\n\n上下文信息: {context}\n\n需求: {requirements}\n\n【全局摘要（用于术语对齐，可为空）】\n{_clip(global_summary or '无', 2200)}\n\n【该页文本抽取（用于术语对齐，可能为空）】\n{_clip(page_text or '无', 5000)}\n\n【已生成用例标题（严禁重复）】\n{avoid}\n\n补充要求：\n- 还需要补充 {int(remaining_min)}–{int(remaining_max)} 条。\n- 重点补充：异常提示、边界值、权限/角色、状态流转、重复提交/并发、数据一致性、可用性。\n- 编号从 TC-{tc_start:03d} 开始连续递增。\n\n输出只包含测试用例本身，不要输出任何说明。\n"""

    def _build_screenshot_cases_prompt(
        self,
        context: str,
        requirements: str,
        page_no: int,
        page_text: str,
        global_summary: str,
        scenario_notes: str,
        tc_start: int,
        target_min: int,
        target_max: int,
        recent_titles: List[str],
        only_new: bool = False,
    ) -> str:
        avoid = "\n".join([f"- {t}" for t in (recent_titles or [])[-40:]]).strip() or "- （无）"
        target_min = int(target_min or 0)
        target_max = int(target_max or max(target_min, 1))
        only_new_rule = "只输出新增的测试用例，不要重复已生成标题；不要输出任何解释文字。" if only_new else ""
        return f"""请基于上传的“PDF第 {page_no} 页截图”生成测试用例（不要啰嗦，不要重复）。\n\n上下文信息: {context}\n\n需求: {requirements}\n\n【全局摘要（用于术语对齐，可为空）】\n{_clip(global_summary or '无', 2200)}\n\n【该页文本抽取（用于术语对齐，可能为空）】\n{_clip(page_text or '无', 5000)}\n\n【本页场景要点清单（必须逐条覆盖）】\n{_clip(scenario_notes or '无', 7000)}\n\n【已生成用例标题（避免同一目的重复；可参数化不要硬拆）】\n{avoid}\n\n数量要求（本页）：\n- 目标输出 {target_min}–{target_max} 条（能多写则多写，但不要少于 {target_min} 条）。\n- 场景拆分原则：每个场景至少 1 正向 + 1 反向/异常；涉及输入/规则的，再补充边界/空值/长度/格式。\n- 列表/分页/筛选/排序/导入导出/复制粘贴/撤销重做/权限/状态流转（截图里出现就必须覆盖）。\n- 不要因为怕重复就少写；同目的用例请用“数据组合/参数化”表达。\n- 测试用例编号必须从 TC-{tc_start:03d} 开始连续递增，跨页不重号、不跳号。\n\n输出规则：\n{only_new_rule}\n\n**重要格式要求**：\n1) 每个测试用例必须以二级标题开始：## TC-001: 测试标题\n2) 每个测试用例必须包含字段（加粗）：\n   - **优先级:** 高/中/低\n   - **描述:** 一行\n   - **前置条件:** 无/...\n3) 测试步骤必须使用标准Markdown表格格式：\n\n### 测试步骤\n\n| # | 步骤描述 | 预期结果 |\n| --- | --- | --- |\n| 1 | ... | ... |\n\n质量要求：\n- 覆盖维度：正向、负向、边界、权限/角色、异常提示与可用性。\n- 只基于图与该页文本，不要臆造接口/字段规则；不确定则把假设写入前置条件或描述。"""


    def _compact_pagefacts(self, related_facts: List[Dict[str, Any]], max_pages: int = 6, max_chars: int = 1800) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for it in (related_facts or [])[: int(max_pages)]:
            if not isinstance(it, dict):
                continue
            pno = int(it.get("page") or 0)
            facts = str(it.get("facts") or "").strip()
            if facts and len(facts) > int(max_chars):
                facts = facts[: int(max_chars)] + "\n...(已截断)"
            out.append({"page": pno, "facts": facts})
        return out

    async def _review(self, digest: Dict[str, Any], batch: List[Dict[str, Any]], cases_markdown: str) -> str:
        reviewer_client = get_review_model_client(user=self.user)
        reviewer_system, prompt = self._build_review_prompt(digest, batch, cases_markdown)
        agent = self._agent(reviewer_client, reviewer_system, "review_agent")
        return await self._run_to_text(agent, prompt)

    async def _revise(self, digest: Dict[str, Any], batch: List[Dict[str, Any]], draft: str, review: str, tc_start: int) -> str:
        revise_system, prompt = self._build_revise_prompt(digest, batch, draft, review, tc_start)
        agent = self._agent(get_text_model_client(user=self.user), revise_system, "revise_agent")
        return await self._run_to_text(agent, prompt)

    async def _write_batch_draft(self, digest: Dict[str, Any], batch: List[Dict[str, Any]], tc_start: int) -> str:
        writer_system, prompt = self._build_writer_prompt(digest, batch, tc_start)
        agent = self._agent(get_text_model_client(user=self.user), writer_system, "writer_agent")
        return await self._run_to_text(agent, prompt)

    async def generate_from_pdf_stream(
        self,
        pdf_path: str,
        context: str,
        requirements: str,
        cancel_event=None,
    ) -> AsyncGenerator[str, None]:
        if cancel_event is not None and cancel_event.is_set():
            yield "\n\n**已停止**\n"
            return

        yield "# 正在生成测试用例...\n\n"
        yield "**PDF按页生成（截图式）**\n"
        use_pagewise = bool(getattr(settings, "AI_PDF_PAGEWISE", True))
        mode = str(getattr(settings, "AI_PDF_PAGEWISE_MODE", "balanced") or "balanced").strip().lower()
        if mode not in ("fast", "balanced", "full"):
            mode = "balanced"
        max_pages = int(getattr(settings, "AI_PDF_PAGEWISE_MAX_PAGES", 80) or 80)
        enable_ocr = bool(getattr(settings, "AI_PDF_PAGEWISE_OCR", False)) if use_pagewise else False
        structured = await pdf_service.extract_structured_from_pdf(
            pdf_path,
            enable_ocr=enable_ocr,
            max_pages=max_pages,
            max_ocr_pages=(8 if enable_ocr else 0),
        )
        metadata = structured.get("metadata") or {}
        pages = structured.get("pages") or []
        pages_total = (metadata.get("pages") if isinstance(metadata, dict) else None) or (len(pages) or "未知")
        yield f"- 页数: {pages_total}\n"
        yield f"- 处理页数: {len(pages)}\n\n"

        if use_pagewise:
            if AGImage is None or AGMultiModalMessage is None:
                yield "\n**错误**: 缺少依赖 autogen-agentchat/autogen-core，无法进行多模态截图生成\n"
                return

            system = (
                "你是一个专业的测试用例生成器，擅长基于截图生成全面的测试用例。"
                "必须严格遵循Markdown格式要求，确保系统可解析。"
            )
            agent = self._agent(get_vision_model_client(user=self.user), system, "pdf_pagewise_cases_agent")

            global_summary = ""
            llm_material = structured.get("llm_material") or ""
            if llm_material.strip() and cancel_event is not None and cancel_event.is_set():
                yield "\n\n**已停止**\n"
                return
            if llm_material.strip() and mode != "fast":
                try:
                    yield "- 正在生成全局摘要（用于术语对齐）...\n"
                    sum_system = "你是需求摘要助手。输出紧凑摘要，便于后续截图生成用例时对齐术语与流程。"
                    sum_agent = self._agent(get_text_model_client(user=self.user), sum_system, "pdf_pagewise_summary_agent")
                    sum_prompt = f"""请基于以下PDF结构化文本，输出一份极短摘要（只要标题+列表，尽量控制在 20 行以内）。\n\n输出结构：\n### 关键术语/模块\n- ...\n\n### 关键流程/状态\n- ...\n\n### 关键约束/校验\n- ...\n\n【PDF结构化文本】\n{_clip(llm_material, 45000)}\n"""
                    global_summary = (await self._run_to_text(sum_agent, sum_prompt, cancel_event=cancel_event)).strip()
                except Exception:
                    global_summary = ""
                yield "\n"

            tc_start = 1
            recent_titles: List[str] = []
            recent_title_keys = set()
            skip_low = bool(getattr(settings, "AI_PDF_PAGEWISE_SKIP_LOW_VALUE", True)) and mode != "full"
            render_scale = float(getattr(settings, "AI_PDF_PAGEWISE_RENDER_SCALE", 1.4) or 1.4)
            max_dim = int(getattr(settings, "AI_PDF_PAGEWISE_IMAGE_MAX_DIM", 1400) or 1400)
            if mode == "fast":
                render_scale = min(render_scale, 1.2)
                max_dim = min(max_dim, 1200)

            for p in pages:
                if cancel_event is not None and cancel_event.is_set():
                    yield "\n\n**已停止**\n"
                    return
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
                target_min, target_max, min_scenarios = self._page_targets(images_cnt, signal)

                yield f"## 第 P{page_no} 页用例\n\n"
                yield f"- 本页图片数: {images_cnt}\n"
                yield f"- 本页文本信号: {signal}\n\n"

                try:
                    pil_img = await asyncio.to_thread(
                        pdf_service.render_page_image,
                        pdf_path,
                        max(page_no - 1, 0),
                        render_scale,
                    )
                except Exception as e:
                    yield f"**错误**: 第 P{page_no} 页渲染失败: {str(e)}\n\n---\n\n"
                    continue
                pil_img = self._downscale_image(pil_img, max_dim)

                page_titles: List[str] = []
                page_title_keys = set()

                combined_prompt = self._build_page_combined_prompt(
                    context=context or "",
                    requirements=requirements or "",
                    page_no=page_no,
                    page_text=page_text,
                    global_summary=global_summary,
                    tc_start=tc_start,
                    target_min=target_min,
                    target_max=target_max,
                    min_scenarios=min_scenarios,
                    recent_titles=recent_titles,
                    only_new=False,
                )
                combined_mm = AGMultiModalMessage(content=[combined_prompt, AGImage(pil_img)], source="user")
                renumber = _TcRenumberTransformer(tc_start)
                collector = _StreamCollector(agent, combined_mm, transformer=renumber, cancel_event=cancel_event)
                async for chunk in collector.run():
                    yield chunk
                yield "\n"

                produced = max(0, renumber.last_no - tc_start + 1) if renumber.last_no >= tc_start else 0
                if renumber.last_no >= tc_start:
                    tc_start = renumber.last_no + 1
                for t in renumber.titles:
                    k = _norm_key(t)
                    if not k or k in recent_title_keys:
                        continue
                    page_title_keys.add(k)
                    page_titles.append(t)

                if mode != "fast" and produced < target_min:
                    remaining_min = max(1, target_min - produced)
                    remaining_max = max(remaining_min, target_max - produced)
                    cont_prompt = self._build_page_continue_prompt(
                        context=context or "",
                        requirements=requirements or "",
                        page_no=page_no,
                        page_text=page_text,
                        global_summary=global_summary,
                        tc_start=tc_start,
                        remaining_min=remaining_min,
                        remaining_max=remaining_max,
                        recent_titles=(recent_titles + page_titles)[-80:],
                    )
                    cont_mm = AGMultiModalMessage(content=[cont_prompt, AGImage(pil_img)], source="user")
                    renumber2 = _TcRenumberTransformer(tc_start)
                    collector2 = _StreamCollector(agent, cont_mm, transformer=renumber2, cancel_event=cancel_event)
                    async for chunk in collector2.run():
                        yield chunk
                    yield "\n"
                    if renumber2.last_no >= tc_start:
                        tc_start = renumber2.last_no + 1
                    for t in renumber2.titles:
                        k = _norm_key(t)
                        if not k or k in recent_title_keys or k in page_title_keys:
                            continue
                        page_title_keys.add(k)
                        page_titles.append(t)

                for t in page_titles:
                    k = _norm_key(t)
                    if not k or k in recent_title_keys:
                        continue
                    recent_title_keys.add(k)
                    recent_titles.append(t)
                    if len(recent_titles) > 60:
                        drop = recent_titles.pop(0)
                        recent_title_keys.discard(_norm_key(drop))
                yield "\n---\n\n"
            return

        llm_material = structured.get("llm_material") or ""
        if not llm_material.strip():
            yield "**错误**: 未能从PDF提取到可用文本\n"
            return

        yield "---\n\n"
        yield "**需求理解摘要（流式）**\n"
        understand_system = "你是需求分析专家。基于结构化文本提取关键术语、模块、流程与约束，输出简洁、可复用的摘要。"
        understand_agent = self._agent(get_text_model_client(user=self.user), understand_system, "pdf_understand_agent")
        understand_prompt = f"""请基于以下PDF结构化文本，输出一份可用于后续图文互证的摘要（不要输出JSON，使用Markdown标题+列表，内容紧凑）。\n\n输出结构：\n### 需求理解摘要\n- ...\n\n### 关键术语/字段词表\n- 术语: 含义/出现位置\n\n### 主要模块与流程\n- ...\n\n### 关键约束/校验\n- ...\n\n【PDF结构化文本】\n{_clip(llm_material, 45000)}\n"""
        understand_collector = _StreamCollector(understand_agent, understand_prompt, cancel_event=cancel_event)
        async for chunk in understand_collector.run():
            yield chunk
        global_understanding = understand_collector.text.strip()

        if cancel_event is not None and cancel_event.is_set():
            yield "\n\n**已停止**\n"
            return

        yield "\n---\n\n"
        yield "**图文互证（PageFacts，流式）**\n"
        if AGImage is None or AGMultiModalMessage is None:
            yield "\n**错误**: 缺少依赖 autogen-agentchat/autogen-core，无法进行多模态图文互证\n"
            return

        page_candidates: List[Dict[str, Any]] = []
        for p in pages:
            try:
                if int((p or {}).get("images") or 0) > 0:
                    page_candidates.append(p)
            except Exception:
                continue
        page_candidates.sort(key=lambda x: int((x or {}).get("images") or 0), reverse=True)
        max_visual_pages = 10
        selected_pages = page_candidates[:max_visual_pages]
        if not selected_pages:
            fallback_n = min(5, int(pages_total) if str(pages_total).isdigit() else len(pages)) or 3
            selected_pages = pages[: int(fallback_n)]
            yield f"- 含图页数: 0（fallback 取前 {len(selected_pages)} 页做图文互证）\n\n"
        else:
            yield f"- 含图页数: {len(page_candidates)}（取前 {len(selected_pages)} 页）\n\n"

        vision_system = "你是资深需求分析助手。必须基于图文共同证据得出结论；只要文本能解释图，就不要写不确定项。"
        vision_agent = self._agent(get_vision_model_client(user=self.user), vision_system, "pdf_pagefacts_agent")

        page_facts: List[Dict[str, Any]] = []
        for p in selected_pages:
            if cancel_event is not None and cancel_event.is_set():
                yield "\n\n**已停止**\n"
                return
            page_no = int((p or {}).get("page") or 0)
            page_index = int((p or {}).get("page") or 1) - 1
            try:
                page_index = int((p or {}).get("page") or 1) - 1
            except Exception:
                page_index = max(page_no - 1, 0)
            page_text = (str((p or {}).get("text") or "").strip() + "\n" + str((p or {}).get("tables") or "").strip()).strip()
            yield f"### P{page_no} PageFacts（图文互证）\n\n"
            yield f"- 正在渲染并对齐 P{page_no}...\n"
            pil_img = await asyncio.to_thread(pdf_service.render_page_image, pdf_path, page_index, 2.0)
            prompt = f"""请对齐“截图 + 该页文本抽取 + 全局词表/流程摘要”，输出该页的统一事实（不要输出JSON，使用Markdown小标题+列表）。\n\n规则：\n1) 先用文本抽取与全局词表去解释截图中的字段/按钮/状态/流程节点。\n2) 只有当“截图+文本抽取+词表/摘要”都无法解释时，才允许写入“仍未消解的问题”。\n3) 不要臆造接口/字段规则。\n\n输出结构（必须包含）：\n#### 图文一致结论\n- ...\n\n#### 仅图侧信息（截图明确可见）\n- ...\n\n#### 仅文侧信息（文本明确写出）\n- ...\n\n#### 可测场景要点（图文互证后）\n- 正向：...\n- 异常：...\n- 边界：...\n- 权限/状态：...\n\n#### 仍未消解的问题（尽量少）\n- ...\n\n【全局词表/流程摘要】\n{_clip(global_understanding, 8000)}\n\n【该页文本抽取】\n{_clip(page_text, 8000)}\n"""
            mm = AGMultiModalMessage(content=[prompt, AGImage(pil_img)], source="user")
            facts_collector = _StreamCollector(vision_agent, mm, cancel_event=cancel_event)
            async for chunk in facts_collector.run():
                yield chunk
            facts_text = facts_collector.text.strip()
            if facts_text:
                page_facts.append({"page": page_no, "facts": facts_text})
            yield "\n\n"

        extracted_text_parts: List[str] = []
        for p in pages:
            page_no = int((p or {}).get("page") or 0)
            txt = str((p or {}).get("text") or "").strip()
            tbl = str((p or {}).get("tables") or "").strip()
            block = "\n".join([x for x in [txt, tbl] if x]).strip()
            if block:
                extracted_text_parts.append(f"\n--- 第 {page_no} 页 ---\n{block}\n")
        extracted_text = "".join(extracted_text_parts).strip()

        digest = {
            "metadata": metadata,
            "text": extracted_text,
            "images": page_facts,
            "images_pages_total": len(page_candidates),
            "images_pages_used": len(selected_pages),
            "context": context or "",
            "requirements": requirements or "",
        }

        yield "---\n\n"
        yield "**功能点拆分（流式进度）**\n"
        chunks = self._split_text_into_chunks(extracted_text)
        if not chunks:
            yield "- text_chunks=0\n\n"
            yield "**错误**: 未能从材料中拆分出功能点清单\n"
            return
        yield f"- text_chunks={len(chunks)}\n"

        split_system = (
            "你是需求分析与测试专家。任务是把材料拆成可测试的功能点清单。"
            "必须避免臆测；不确定的点要写进 uncertainties，而不是当成需求。"
        )
        split_agent = self._agent(get_text_model_client(user=self.user), split_system, "func_point_agent")
        all_points: List[Dict[str, Any]] = []
        zero_chunks = 0
        for idx, ch in enumerate(chunks, 1):
            if cancel_event is not None and cancel_event.is_set():
                yield "\n\n**已停止**\n"
                return
            pages_in = [p for p in (ch.get("pages") or []) if isinstance(p, int) and p > 0]
            min_p = min(pages_in) if pages_in else 0
            max_p = max(pages_in) if pages_in else 0
            related_facts = []
            if min_p and max_p:
                for it in page_facts:
                    if isinstance(it, dict):
                        pno = int(it.get("page") or 0)
                        if min_p <= pno <= max_p:
                            related_facts.append(it)
            compact_facts = self._compact_pagefacts(related_facts)
            yield f"- 正在分析分块 {idx}/{len(chunks)}（pages {min_p}-{max_p}）...\n"
            prompt = f"""请仅基于以下材料拆分“需求功能点清单”。\n\n输出要求：\n- 只输出一个 JSON 数组，必须用 BEGIN_JSON 与 END_JSON 包裹；除此之外不要输出任何文字。\n- 不要使用```代码块。\n\nBEGIN_JSON\n[\n  {{\n    \"fid\": \"F01\",\n    \"title\": \"功能点标题\",\n    \"description\": \"一句话说明\",\n    \"complexity\": \"simple|medium|complex\",\n    \"evidence\": [{{\"type\":\"text|pagefacts\",\"page\":1,\"quote\":\"原文短句或PageFacts原句\"}}],\n    \"uncertainties\": [\"不确定项\"]\n  }}\n]\nEND_JSON\n\n规则：\n1) 数量原则：按复杂度自动决定功能点数量，不设上限；不要为了凑整重复。\n2) 粒度：拆到可测试的业务动作/规则层级，禁止模板/占位词。\n3) 证据：每个功能点至少 1 条 evidence；当提供了 PageFacts 时，优先引用 PageFacts 原句作为证据；引用 text 时必须是原文短句（尽量原样摘录）。\n4) 不确定：图文互证后仍无法确认的点，才写 uncertainties。\n\n上下文（可能为空）：\n{digest.get('context') or ''}\n\n补充需求（可能为空）：\n{digest.get('requirements') or ''}\n\n【全局词表/流程摘要】\n{_clip(global_understanding, 4000)}\n\n【分块文本】\n{_clip(ch.get('text') or '', 12000)}\n\n【相关PageFacts（可为空）】\n{_clip(json.dumps(compact_facts, ensure_ascii=False), 12000)}\n"""
            raw = await self._run_to_text(split_agent, prompt, cancel_event=cancel_event)
            pts = _extract_json_array(raw)
            if not pts:
                raw2 = await self._run_to_text(
                    split_agent,
                    "把刚才的输出转换为严格 JSON 数组，并且只输出 BEGIN_JSON 与 END_JSON 包裹的那部分。",
                    cancel_event=cancel_event,
                )
                pts = _extract_json_array(raw2)
                if not pts:
                    pts = _extract_points_from_bullets(raw or raw2)
            if not pts:
                zero_chunks += 1
            yield f"- 完成分块 {idx}/{len(chunks)}，提取到 {len(pts)} 个功能点\n"
            all_points.extend(pts)

        points = self._dedup_points(all_points)
        yield "\n"

        if len(points) < 6 or zero_chunks >= max(2, int(len(chunks) / 2)):
            yield f"- 低产出（points={len(points)}, zero_chunks={zero_chunks}），启用一次性提取...\n"
            one_shot_prompt = f"""请基于以下材料一次性拆分“需求功能点清单”。\n\n输出要求：\n- 只输出一个 JSON 数组，必须用 BEGIN_JSON 与 END_JSON 包裹；除此之外不要输出任何文字。\n- 不要使用```代码块。\n\nBEGIN_JSON\n[\n  {{\n    \"fid\": \"F01\",\n    \"title\": \"功能点标题\",\n    \"description\": \"一句话说明\",\n    \"complexity\": \"simple|medium|complex\",\n    \"evidence\": [{{\"type\":\"text|pagefacts\",\"page\":1,\"quote\":\"原文短句或PageFacts原句\"}}],\n    \"uncertainties\": [\"不确定项\"]\n  }}\n]\nEND_JSON\n\n规则：\n1) 输出尽量全面，不要为了凑整重复。\n2) 每个功能点必须至少 1 条 evidence。\n3) 当提供了 PageFacts 时，优先引用 PageFacts 原句作为证据；引用 text 时必须是原文短句。\n4) 只有当图文互证后仍无法确认，才写 uncertainties。\n\n上下文（可能为空）：\n{digest.get('context') or ''}\n\n补充需求（可能为空）：\n{digest.get('requirements') or ''}\n\n【全局词表/流程摘要】\n{_clip(global_understanding, 6000)}\n\n【PageFacts 汇总】\n{_clip(json.dumps(self._compact_pagefacts(page_facts, max_pages=10, max_chars=1400), ensure_ascii=False), 20000)}\n\n【全文文本（分页）】\n{_clip(extracted_text, 35000)}\n"""
            raw = await self._run_to_text(split_agent, one_shot_prompt, cancel_event=cancel_event)
            more = _extract_json_array(raw)
            if not more:
                raw2 = await self._run_to_text(
                    split_agent,
                    "把刚才的输出转换为严格 JSON 数组，并且只输出 BEGIN_JSON 与 END_JSON 包裹的那部分。",
                    cancel_event=cancel_event,
                )
                more = _extract_json_array(raw2)
                if not more:
                    more = _extract_points_from_bullets(raw or raw2)
            points = self._dedup_points(points + more)
            yield f"- 一次性提取后 points={len(points)}\n\n"

        if not points:
            yield "**错误**: 未能从材料中拆分出功能点清单\n"
            return

        yield "## 需求功能点清单\n\n"
        for fp in points[:160]:
            title = str(fp.get("title") or "").strip()
            fid = str(fp.get("fid") or "").strip()
            if title:
                yield f"- {fid + ' ' if fid else ''}{title}\n"
        tmin, tmax = self._batch_targets(points)
        yield f"\n**统计**: 功能点 {len(points)} 个；建议用例总数约 {tmin}–{tmax} 条。\n\n"
        yield "---\n\n"

        batch_size = 5
        tc_start = 1
        for bi in range(0, len(points), batch_size):
            batch = points[bi : bi + batch_size]
            if not batch:
                continue
            bmin, bmax = self._batch_targets(batch)
            yield f"## 用例批次 {bi // batch_size + 1}（建议 {bmin}–{bmax} 条）\n\n"

            yield "### 草稿生成（DRAFT，不入左侧用例列表）\n\n"
            writer_system, writer_prompt = self._build_writer_prompt(digest, batch, tc_start)
            writer_agent = self._agent(get_text_model_client(user=self.user), writer_system, "writer_agent")
            draft_collector = _StreamCollector(
                writer_agent,
                writer_prompt,
                transformer=_HeadingPrefixTransformer("DRAFT"),
                cancel_event=cancel_event,
            )
            async for chunk in draft_collector.run():
                yield chunk
            draft = draft_collector.text
            if not draft.strip():
                yield "\n\n**错误**: 草稿生成为空\n\n"
                return

            yield "\n\n### 自动评审（流式）\n\n"
            reviewer_client = get_review_model_client(user=self.user)
            review_system, review_prompt = self._build_review_prompt(digest, batch, draft)
            review_agent = self._agent(reviewer_client, review_system, "review_agent")
            review_collector = _StreamCollector(review_agent, review_prompt, cancel_event=cancel_event)
            async for chunk in review_collector.run():
                yield chunk
            review = review_collector.text

            yield "\n\n### 修订生成（TEMP，不入左侧用例列表）\n\n"
            revise_system, revise_prompt = self._build_revise_prompt(digest, batch, draft, review, tc_start)
            revise_agent = self._agent(get_text_model_client(user=self.user), revise_system, "revise_agent")
            temp_collector = _StreamCollector(
                revise_agent,
                revise_prompt,
                transformer=_HeadingPrefixTransformer("TEMP"),
                cancel_event=cancel_event,
            )
            async for chunk in temp_collector.run():
                yield chunk
            revised_raw = temp_collector.text

            yield "\n\n### 最终用例（已整理，入左侧用例列表）\n\n"
            revised = normalize_case_headings(revised_raw)
            revised = sort_case_blocks(revised)
            revised = fix_incomplete_last_case(revised)
            yield revised
            yield "\n---\n\n"

            last_no = _find_last_tc_no(revised)
            tc_start = (last_no + 1) if last_no else (tc_start + max(1, bmin))

    async def generate_from_text_stream(
        self,
        title: str,
        material_text: str,
        context: str,
        requirements: str,
        cancel_event=None,
    ) -> AsyncGenerator[str, None]:
        digest = {
            "metadata": {"title": title or ""},
            "text": (material_text or "").strip(),
            "images": [],
            "images_pages_total": 0,
            "images_pages_used": 0,
            "context": context or "",
            "requirements": requirements or "",
        }

        if cancel_event is not None and cancel_event.is_set():
            yield "\n\n**已停止**\n"
            return

        yield "# 正在生成测试用例...\n\n"
        yield "**材料信息**\n"
        if title:
            yield f"- 标题: {title}\n"
        yield f"- 文本长度: {len(digest['text'])}\n\n"
        yield "---\n\n"

        yield "**功能点拆分（流式进度）**\n"
        extracted_text = digest.get("text") or ""
        chunks = self._split_text_into_chunks(extracted_text)
        if not chunks:
            yield "- text_chunks=0\n\n"
            yield "**错误**: 未能从材料中拆分出功能点清单\n"
            return
        yield f"- text_chunks={len(chunks)}\n"

        system = (
            "你是需求分析与测试专家。任务是把材料拆成可测试的功能点清单。"
            "必须避免臆测；不确定的点要写进 uncertainties，而不是当成需求。"
        )
        agent = self._agent(get_text_model_client(user=self.user), system, "func_point_agent")
        all_points: List[Dict[str, Any]] = []
        zero_chunks = 0
        for idx, ch in enumerate(chunks, 1):
            if cancel_event is not None and cancel_event.is_set():
                yield "\n\n**已停止**\n"
                return
            yield f"- 正在分析分块 {idx}/{len(chunks)}...\n"
            prompt = f"""请仅基于以下材料拆分“需求功能点清单”。

输出要求：
- 只输出一个 JSON 数组，必须用 BEGIN_JSON 与 END_JSON 包裹；除此之外不要输出任何文字。
- 不要使用```代码块。

BEGIN_JSON
[
  {{
    "fid": "F01",
    "title": "功能点标题",
    "description": "一句话说明",
    "complexity": "simple|medium|complex",
    "evidence": [{{"type":"text","page":0,"quote":"原文短句"}}],
    "uncertainties": ["不确定项"]
  }}
]
END_JSON

规则：
1) 数量原则：按复杂度自动决定功能点数量，不设上限；不要为了凑整重复。
2) 粒度：拆到可测试的业务动作/规则层级，禁止模板/占位词。
3) 证据：每个功能点至少 1 条 evidence；必须是原文短句（尽量原样摘录）。
4) 不确定：材料缺失就放 uncertainties，不要当成需求。

上下文（可能为空）：
{digest.get('context') or ''}

补充需求（可能为空）：
{digest.get('requirements') or ''}

【分块文本】
{_clip(ch.get('text') or '', 12000)}
"""
            raw = await self._run_to_text(agent, prompt, cancel_event=cancel_event)
            pts = _extract_json_array(raw)
            if not pts:
                raw2 = await self._run_to_text(
                    agent,
                    "把刚才的输出转换为严格 JSON 数组，并且只输出 BEGIN_JSON 与 END_JSON 包裹的那部分。",
                    cancel_event=cancel_event,
                )
                pts = _extract_json_array(raw2)
                if not pts:
                    pts = _extract_points_from_bullets(raw or raw2)
            if not pts:
                zero_chunks += 1
            yield f"- 完成分块 {idx}/{len(chunks)}，提取到 {len(pts)} 个功能点\n"
            all_points.extend(pts)
        points = self._dedup_points(all_points)
        yield "\n"

        if len(points) < 6 or zero_chunks >= max(2, int(len(chunks) / 2)):
            yield f"- 低产出（points={len(points)}, zero_chunks={zero_chunks}），启用一次性提取...\n"
            one_shot_prompt = f"""请基于以下材料一次性拆分“需求功能点清单”。\n\n输出要求：\n- 只输出一个 JSON 数组，必须用 BEGIN_JSON 与 END_JSON 包裹；除此之外不要输出任何文字。\n- 不要使用```代码块。\n\nBEGIN_JSON\n[\n  {{\n    \"fid\": \"F01\",\n    \"title\": \"功能点标题\",\n    \"description\": \"一句话说明\",\n    \"complexity\": \"simple|medium|complex\",\n    \"evidence\": [{{\"type\":\"text\",\"page\":0,\"quote\":\"原文短句\"}}],\n    \"uncertainties\": [\"不确定项\"]\n  }}\n]\nEND_JSON\n\n规则：\n1) 输出尽量全面，不要为了凑整重复。\n2) 每个功能点必须至少 1 条 evidence（原文短句）。\n3) 只有当材料缺失时，才写 uncertainties。\n\n上下文（可能为空）：\n{digest.get('context') or ''}\n\n补充需求（可能为空）：\n{digest.get('requirements') or ''}\n\n【全文文本】\n{_clip(extracted_text, 45000)}\n"""
            raw = await self._run_to_text(agent, one_shot_prompt, cancel_event=cancel_event)
            more = _extract_json_array(raw)
            if not more:
                raw2 = await self._run_to_text(
                    agent,
                    "把刚才的输出转换为严格 JSON 数组，并且只输出 BEGIN_JSON 与 END_JSON 包裹的那部分。",
                    cancel_event=cancel_event,
                )
                more = _extract_json_array(raw2)
                if not more:
                    more = _extract_points_from_bullets(raw or raw2)
            points = self._dedup_points(points + more)
            yield f"- 一次性提取后 points={len(points)}\n\n"

        if not points:
            yield "**错误**: 未能从材料中拆分出功能点清单\n"
            return

        yield "## 需求功能点清单\n\n"
        for fp in points[:160]:
            title = str(fp.get("title") or "").strip()
            fid = str(fp.get("fid") or "").strip()
            if title:
                yield f"- {fid + ' ' if fid else ''}{title}\n"
        tmin, tmax = self._batch_targets(points)
        yield f"\n**统计**: 功能点 {len(points)} 个；建议用例总数约 {tmin}–{tmax} 条。\n\n"
        yield "---\n\n"

        batch_size = 5
        tc_start = 1
        for bi in range(0, len(points), batch_size):
            batch = points[bi : bi + batch_size]
            if not batch:
                continue
            if cancel_event is not None and cancel_event.is_set():
                yield "\n\n**已停止**\n"
                return
            bmin, bmax = self._batch_targets(batch)
            yield f"## 用例批次 {bi // batch_size + 1}（建议 {bmin}–{bmax} 条）\n\n"
            yield "### 草稿生成（DRAFT，不入左侧用例列表）\n\n"
            writer_system, writer_prompt = self._build_writer_prompt(digest, batch, tc_start)
            writer_agent = self._agent(get_text_model_client(user=self.user), writer_system, "writer_agent")
            draft_collector = _StreamCollector(
                writer_agent,
                writer_prompt,
                transformer=_HeadingPrefixTransformer("DRAFT"),
                cancel_event=cancel_event,
            )
            async for chunk in draft_collector.run():
                yield chunk
            draft = draft_collector.text
            if not draft.strip():
                yield "\n\n**错误**: 草稿生成为空\n\n"
                return

            yield "\n\n### 自动评审（流式）\n\n"
            reviewer_client = get_review_model_client(user=self.user)
            review_system, review_prompt = self._build_review_prompt(digest, batch, draft)
            review_agent = self._agent(reviewer_client, review_system, "review_agent")
            review_collector = _StreamCollector(review_agent, review_prompt, cancel_event=cancel_event)
            async for chunk in review_collector.run():
                yield chunk
            review = review_collector.text

            yield "\n\n### 修订生成（TEMP，不入左侧用例列表）\n\n"
            revise_system, revise_prompt = self._build_revise_prompt(digest, batch, draft, review, tc_start)
            revise_agent = self._agent(get_text_model_client(user=self.user), revise_system, "revise_agent")
            temp_collector = _StreamCollector(
                revise_agent,
                revise_prompt,
                transformer=_HeadingPrefixTransformer("TEMP"),
                cancel_event=cancel_event,
            )
            async for chunk in temp_collector.run():
                yield chunk
            revised_raw = temp_collector.text

            yield "\n\n### 最终用例（已整理，入左侧用例列表）\n\n"
            revised = normalize_case_headings(revised_raw)
            revised = sort_case_blocks(revised)
            revised = fix_incomplete_last_case(revised)
            yield revised
            yield "\n---\n\n"

            last_no = _find_last_tc_no(revised)
            tc_start = (last_no + 1) if last_no else (tc_start + max(1, bmin))
