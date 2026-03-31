from __future__ import annotations

import re
from typing import Dict, List, Tuple


_TITLE_RE = re.compile(r"^#{0,6}\s*\*{0,2}TC-([\w-]+)[:：]?\s*(.*?)\*{0,2}\s*$", re.I)
_SEP_RE = re.compile(r"^\s*\|?[\s\-:]+\|[\s\-:]+")


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def parse_cases_from_markdown(markdown_text: str) -> List[Dict]:
    cases: List[Dict] = []
    lines = (markdown_text or "").splitlines()
    current: Dict | None = None
    in_table = False

    def save():
        nonlocal current
        if not current:
            return
        if current.get("title") and current.get("steps_list"):
            cases.append(current)
        current = None

    for raw_line in lines:
        line = raw_line.strip()
        m = _TITLE_RE.match(line)
        if m:
            if current:
                save()
            current = {
                "title": f"TC-{m.group(1)}: {m.group(2)}".strip(),
                "pre_condition": "",
                "priority": "",
                "description": "",
                "steps_list": [],
            }
            in_table = False
            continue

        if not current:
            continue

        m_field = re.match(r"^\s*(?:[-*]\s*)?\*{0,2}\s*优先级\s*\*{0,2}\s*[:：]\s*(.*)\s*$", line)
        if m_field:
            current["priority"] = (m_field.group(1) or "").strip()
            continue

        m_field = re.match(r"^\s*(?:[-*]\s*)?\*{0,2}\s*描述\s*\*{0,2}\s*[:：]\s*(.*)\s*$", line)
        if m_field:
            current["description"] = (m_field.group(1) or "").strip()
            continue

        m_field = re.match(r"^\s*(?:[-*]\s*)?\*{0,2}\s*前置条件\s*\*{0,2}\s*[:：]\s*(.*)\s*$", line)
        if m_field:
            current["pre_condition"] = (m_field.group(1) or "").strip()
            continue

        if _SEP_RE.match(line):
            in_table = True
            continue

        if in_table:
            if "|" not in line:
                if line != "":
                    in_table = False
                continue
            if (("步骤" in line) or ("Step" in line)) and (("预期" in line) or ("Result" in line)):
                continue

            cols = [c.strip() for c in line.split("|")]
            if cols and cols[0] == "":
                cols = cols[1:]
            if cols and cols[-1] == "":
                cols = cols[:-1]

            if len(cols) >= 2:
                if len(cols) >= 3:
                    step = cols[1]
                    result = cols[2]
                else:
                    step = cols[0]
                    result = cols[1]
                if step.strip():
                    current["steps_list"].append({"description": step.strip(), "expected_result": result.strip()})

    if current:
        save()
    return cases


def cases_to_markdown(cases: List[Dict], tc_start: int = 1) -> Tuple[str, int]:
    out: List[str] = []
    no = int(tc_start or 1)
    for case in cases:
        title = str(case.get("title") or "").strip()
        tail = title.split(":", 1)[1].strip() if ":" in title else title
        out.append(f"## TC-{no:03d}: {tail}".strip())
        prio = str(case.get("priority") or "").strip() or "中"
        desc = str(case.get("description") or "").strip() or "无"
        pre = str(case.get("pre_condition") or "").strip() or "无"
        out.append("")
        out.append(f"**优先级:** {prio}")
        out.append("")
        out.append(f"**描述:** {desc}")
        out.append("")
        out.append(f"**前置条件:** {pre}")
        out.append("")
        out.append("### 测试步骤")
        out.append("")
        out.append("| # | 步骤描述 | 预期结果 |")
        out.append("| --- | --- | --- |")
        steps = case.get("steps_list") or []
        for i, step in enumerate(steps):
            s = str((step or {}).get("description") or "").strip()
            r = str((step or {}).get("expected_result") or "").strip()
            if not s:
                continue
            out.append(f"| {i+1} | {s} | {r} |")
        out.append("")
        no += 1
    return "\n".join(out).rstrip() + ("\n" if out else ""), no - 1


def ensure_markdown_parseable(markdown_text: str, tc_start: int = 1) -> Tuple[str, int]:
    cases = parse_cases_from_markdown(markdown_text or "")
    if cases:
        return cases_to_markdown(cases, tc_start=tc_start)
    table_steps = _extract_table_steps(markdown_text or "")
    if table_steps:
        cases = [
            {
                "title": "TC-001: AI生成用例",
                "priority": "中",
                "description": "无",
                "pre_condition": "无",
                "steps_list": table_steps,
            }
        ]
        return cases_to_markdown(cases, tc_start=tc_start)
    return "", tc_start - 1


def _extract_table_steps(markdown_text: str) -> List[Dict]:
    steps: List[Dict] = []
    in_table = False
    for raw_line in (markdown_text or "").splitlines():
        line = raw_line.strip()
        if _SEP_RE.match(line):
            in_table = True
            continue
        if not in_table:
            continue
        if "|" not in line:
            if line != "":
                in_table = False
            continue
        if (("步骤" in line) or ("Step" in line)) and (("预期" in line) or ("Result" in line)):
            continue
        cols = [c.strip() for c in line.split("|")]
        if cols and cols[0] == "":
            cols = cols[1:]
        if cols and cols[-1] == "":
            cols = cols[:-1]
        if len(cols) >= 2:
            if len(cols) >= 3:
                step = cols[1]
                result = cols[2]
            else:
                step = cols[0]
                result = cols[1]
            if step.strip():
                steps.append({"description": step.strip(), "expected_result": result.strip()})
    return steps


def dedup_cases(cases: List[Dict]) -> List[Dict]:
    seen = set()
    out: List[Dict] = []
    for c in cases or []:
        title = _norm(str(c.get("title") or ""))
        steps = c.get("steps_list") or []
        step_sig = "|".join([_norm(str((s or {}).get("description") or "")) for s in steps])[:2000]
        key = (title, step_sig)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out

