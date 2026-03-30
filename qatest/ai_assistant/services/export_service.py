import io
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any


def _as_text(v: Any) -> str:
    if v is None:
        return ""
    try:
        return str(v)
    except Exception:
        return ""


def _excel_col_name(n: int) -> str:
    s = ""
    x = n
    while x > 0:
        x, r = divmod(x - 1, 26)
        s = chr(65 + r) + s
    return s


def generate_xlsx_bytes(cases: list[dict]) -> bytes:
    rows: list[list[Any]] = []
    rows.append(["用例标题", "前置条件", "优先级", "步骤序号", "步骤描述", "预期结果"])
    for c in cases or []:
        title = _as_text(c.get("title"))
        pre = _as_text(c.get("pre_condition"))
        prio = _as_text(c.get("priority"))
        steps = c.get("steps_list") or []
        if not steps:
            rows.append([title, pre, prio, "", "", ""])
            continue
        for idx, st in enumerate(steps, 1):
            rows.append(
                [
                    title if idx == 1 else "",
                    pre if idx == 1 else "",
                    prio if idx == 1 else "",
                    idx,
                    _as_text((st or {}).get("description")),
                    _as_text((st or {}).get("expected_result")),
                ]
            )

    def cell_xml(r: int, c: int, value: Any) -> str:
        ref = f"{_excel_col_name(c)}{r}"
        if value is None or value == "":
            return ""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f'<c r="{ref}" t="n"><v>{value}</v></c>'
        text = _as_text(value)
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'

    sheet_rows = []
    for r_idx, row in enumerate(rows, 1):
        cells = "".join(cell_xml(r_idx, c_idx, v) for c_idx, v in enumerate(row, 1))
        sheet_rows.append(f'<row r="{r_idx}">{cells}</row>')

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        + "".join(sheet_rows)
        + "</sheetData></worksheet>"
    )

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Test Cases" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )

    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )

    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )

    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )

    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", rels_xml)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return bio.getvalue()


def generate_xmind_bytes(cases: list[dict], sheet_title: str = "测试用例") -> bytes:
    ns = "urn:xmind:xmap:xmlns:content:2.0"
    ET.register_namespace("", ns)

    root = ET.Element(f"{{{ns}}}xmap-content")
    sheet = ET.SubElement(root, "sheet", {"id": uuid.uuid4().hex})
    ET.SubElement(sheet, "title").text = sheet_title

    root_topic = ET.SubElement(sheet, "topic", {"id": uuid.uuid4().hex})
    ET.SubElement(root_topic, "title").text = "测试用例集"
    root_children = ET.SubElement(root_topic, "children")
    root_attached = ET.SubElement(root_children, "topics", {"type": "attached"})

    def ensure_children(topic_el):
        ch = topic_el.find("children")
        if ch is None:
            ch = ET.SubElement(topic_el, "children")
        tp = ch.find("topics")
        if tp is None:
            tp = ET.SubElement(ch, "topics", {"type": "attached"})
        return tp

    for c in cases or []:
        case_topic = ET.SubElement(root_attached, "topic", {"id": uuid.uuid4().hex})
        ET.SubElement(case_topic, "title").text = _as_text(c.get("title")) or "未命名用例"

        attached = ensure_children(case_topic)

        pre = _as_text(c.get("pre_condition")).strip()
        prio = _as_text(c.get("priority")).strip()
        if prio:
            t = ET.SubElement(attached, "topic", {"id": uuid.uuid4().hex})
            ET.SubElement(t, "title").text = f"优先级: {prio}"
        if pre:
            t = ET.SubElement(attached, "topic", {"id": uuid.uuid4().hex})
            ET.SubElement(t, "title").text = f"前置条件: {pre}"

        steps = c.get("steps_list") or []
        if steps:
            steps_topic = ET.SubElement(attached, "topic", {"id": uuid.uuid4().hex})
            ET.SubElement(steps_topic, "title").text = "测试步骤"
            steps_attached = ensure_children(steps_topic)
            for i, st in enumerate(steps, 1):
                step_topic = ET.SubElement(steps_attached, "topic", {"id": uuid.uuid4().hex})
                ET.SubElement(step_topic, "title").text = f"步骤 {i}"
                step_attached = ensure_children(step_topic)
                action = _as_text((st or {}).get("description")).strip()
                exp = _as_text((st or {}).get("expected_result")).strip()
                if action:
                    t = ET.SubElement(step_attached, "topic", {"id": uuid.uuid4().hex})
                    ET.SubElement(t, "title").text = f"操作: {action}"
                if exp:
                    t = ET.SubElement(step_attached, "topic", {"id": uuid.uuid4().hex})
                    ET.SubElement(t, "title").text = f"预期结果: {exp}"

    content_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    meta_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
        '<meta xmlns="urn:xmind:xmap:xmlns:meta:2.0" version="2.0"></meta>'
    )

    manifest_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
        '<manifest xmlns="urn:xmind:xmap:xmlns:manifest:1.0">'
        '<file-entry full-path="content.xml" media-type="text/xml"/>'
        '<file-entry full-path="meta.xml" media-type="text/xml"/>'
        '<file-entry full-path="Thumbnails/" media-type="application/vnd.xmind.thumbnails"/>'
        "</manifest>"
    )

    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.xml", content_bytes)
        zf.writestr("meta.xml", meta_xml)
        zf.writestr("META-INF/manifest.xml", manifest_xml)
        zf.writestr("Thumbnails/", "")
    return bio.getvalue()


def build_export_filename(prefix: str, ext: str) -> str:
    safe_prefix = "".join(ch for ch in (prefix or "test_cases") if ch.isalnum() or ch in ("_", "-", " ", ".", "（", "）", "(", ")")).strip()
    if not safe_prefix:
        safe_prefix = "test_cases"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{safe_prefix}_{ts}.{ext}"

