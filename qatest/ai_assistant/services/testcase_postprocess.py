import re
from typing import List, Tuple


def extract_case_blocks(markdown: str) -> List[Tuple[int, str, str]]:
    text = markdown or ""
    lines = text.splitlines()
    starts: List[int] = []
    pat = re.compile(r"^##\s*TC-(\d{1,6})\s*[:：]", re.IGNORECASE)
    for i, line in enumerate(lines):
        if pat.match(line.strip()):
            starts.append(i)
    if not starts:
        return []
    blocks: List[Tuple[int, str, str]] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        block = "\n".join(lines[start:end]).strip() + "\n"
        m = pat.match(lines[start].strip())
        num = int(m.group(1)) if m else 0
        title = lines[start].strip()
        blocks.append((num, title, block))
    return blocks


def sort_case_blocks(markdown: str) -> str:
    blocks = extract_case_blocks(markdown)
    if len(blocks) < 2:
        return markdown
    blocks.sort(key=lambda x: x[0])
    return "".join([b[2] for b in blocks]).rstrip() + "\n"


def fix_incomplete_last_case(markdown: str) -> str:
    blocks = extract_case_blocks(markdown)
    if not blocks:
        return markdown
    last_num, last_title, last_block = blocks[-1]
    lines = last_block.splitlines()
    table_lines = [ln for ln in lines if ln.strip().startswith("|")]
    if not table_lines:
        return markdown
    header = ""
    for ln in table_lines:
        if "步骤" in ln and "预期" in ln:
            header = ln
            break
    expected_pipes = header.count("|") if header else max([ln.count("|") for ln in table_lines[:2]] + [0])
    if expected_pipes <= 0:
        return markdown
    for i in range(len(lines) - 1, -1, -1):
        ln = lines[i].strip()
        if not ln.startswith("|"):
            continue
        if ln.count("|") < expected_pipes:
            lines = lines[:i]
            break
        break
    fixed_last = "\n".join(lines).rstrip() + "\n"
    rebuilt = "".join([b[2] for b in blocks[:-1]]) + fixed_last
    return rebuilt.rstrip() + "\n"


def normalize_case_headings(markdown: str) -> str:
    text = markdown or ""
    out_lines: List[str] = []
    pat = re.compile(r"^(#{1,6}\s*)?(\*{0,2})?(TC-(\d{1,6}))\s*[:：]\s*(.+?)(\*{0,2})?$", re.IGNORECASE)
    for line in text.splitlines():
        m = pat.match(line.strip())
        if not m:
            out_lines.append(line)
            continue
        tc = m.group(3).upper()
        title = (m.group(5) or "").strip()
        out_lines.append(f"## {tc}: {title}")
    return "\n".join(out_lines).rstrip() + "\n"

