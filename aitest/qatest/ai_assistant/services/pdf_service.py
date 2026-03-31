import os
import PyPDF2
import pdfplumber
from typing import Dict, List, Any, Optional
import re
from datetime import datetime
import asyncio
import io

try:
    import pypdfium2 as pdfium
except Exception:
    pdfium = None

class PDFService:
    def __init__(self):
        self.supported_extensions = ['.pdf']
    
    def extract_text_from_pdf(self, pdf_path: str) -> Dict[str, Any]:
        """
        从PDF文件中提取文本内容
        """
        try:
            # 使用pdfplumber提取文本（更好的表格和布局支持）
            text_content = ""
            metadata = {}
            
            with pdfplumber.open(pdf_path) as pdf:
                # 提取元数据
                if pdf.metadata:
                    metadata = {
                        'title': pdf.metadata.get('Title', ''),
                        'author': pdf.metadata.get('Author', ''),
                        'subject': pdf.metadata.get('Subject', ''),
                        'creator': pdf.metadata.get('Creator', ''),
                        'creation_date': pdf.metadata.get('CreationDate', ''),
                        'modification_date': pdf.metadata.get('ModDate', ''),
                        'pages': len(pdf.pages)
                    }
                
                # 提取每页文本
                for page_num, page in enumerate(pdf.pages, 1):
                    page_text = page.extract_text()
                    if page_text:
                        text_content += f"\n--- 第 {page_num} 页 ---\n"
                        text_content += page_text
                        text_content += "\n"
            
            # 如果pdfplumber失败，尝试使用PyPDF2
            if not text_content.strip():
                text_content = self._extract_with_pypdf2(pdf_path)
            
            return {
                'text': text_content,
                'metadata': metadata,
                'extraction_method': 'pdfplumber' if text_content else 'pypdf2',
                'extracted_at': datetime.now().isoformat()
            }
            
        except Exception as e:
            raise Exception(f"PDF文本提取失败: {str(e)}")

    def list_pages_with_images(self, pdf_path: str, max_pages: int = 200) -> List[Dict[str, Any]]:
        pages: List[Dict[str, Any]] = []
        with pdfplumber.open(pdf_path) as pdf:
            total = min(len(pdf.pages), int(max_pages))
            for i in range(total):
                page = pdf.pages[i]
                count = 0
                try:
                    count = len(getattr(page, "images", []) or [])
                except Exception:
                    count = 0
                pages.append({"page_index": i, "page": i + 1, "images": count})
        return [p for p in pages if int(p.get("images") or 0) > 0]

    def render_page_image(self, pdf_path: str, page_index: int, scale: float = 2.0):
        if pdfium is None:
            raise RuntimeError("缺少依赖 pypdfium2，无法渲染 PDF 页面为图片")
        doc = pdfium.PdfDocument(pdf_path)
        page = doc.get_page(int(page_index))
        pil = page.render(scale=float(scale)).to_pil()
        page.close()
        doc.close()
        return pil

    async def extract_structured_from_pdf(
        self,
        pdf_path: str,
        enable_ocr: bool = True,
        max_pages: int = 80,
        max_ocr_pages: int = 30,
        user=None,
    ) -> Dict[str, Any]:
        def _clean(s: str) -> str:
            return (s or "").replace("\x00", "").strip()

        def _signal_count(s: str) -> int:
            txt = re.sub(r"\s+", "", s or "")
            hits = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", txt)
            return len(hits)

        def _tables_to_text(tables: List[List[List[Any]]]) -> str:
            out: List[str] = []
            for t in (tables or [])[:3]:
                rows = []
                for row in t or []:
                    cells = [str(c or "").strip() for c in (row or [])]
                    if any(cells):
                        rows.append(" | ".join(cells))
                if rows:
                    out.append("\n".join(rows))
            return "\n\n".join(out).strip()

        def _render_page_png_bytes(path: str, page_index: int) -> Optional[bytes]:
            if pdfium is None:
                return None
            doc = pdfium.PdfDocument(path)
            page = doc.get_page(page_index)
            pil = page.render(scale=2).to_pil()
            bio = io.BytesIO()
            pil.save(bio, format="PNG")
            page.close()
            doc.close()
            return bio.getvalue()

        metadata: Dict[str, Any] = {}
        pages: List[Dict[str, Any]] = []
        def _extract_base():
            meta: Dict[str, Any] = {}
            ps: List[Dict[str, Any]] = []
            cands: List[Dict[str, Any]] = []
            with pdfplumber.open(pdf_path) as pdf:
                if pdf.metadata:
                    meta = {
                        'title': pdf.metadata.get('Title', ''),
                        'author': pdf.metadata.get('Author', ''),
                        'subject': pdf.metadata.get('Subject', ''),
                        'creator': pdf.metadata.get('Creator', ''),
                        'creation_date': pdf.metadata.get('CreationDate', ''),
                        'modification_date': pdf.metadata.get('ModDate', ''),
                        'pages': len(pdf.pages),
                    }

                total = min(len(pdf.pages), int(max_pages))
                for i in range(total):
                    page = pdf.pages[i]
                    page_no = i + 1
                    raw_text = _clean(page.extract_text() or "")
                    signal = _signal_count(raw_text)
                    tables_text = ""
                    try:
                        tables_text = _tables_to_text(page.extract_tables() or [])
                    except Exception:
                        tables_text = ""

                    images_count = 0
                    try:
                        images_count = len(getattr(page, "images", []) or [])
                    except Exception:
                        images_count = 0

                    ps.append(
                        {
                            "page": page_no,
                            "text": raw_text,
                            "tables": tables_text,
                            "images": images_count,
                            "signal": signal,
                            "ocr_attempted": False,
                            "ocr_text": "",
                            "ocr_confidence": "",
                            "ocr_warnings": [],
                        }
                    )
                    if enable_ocr and signal < 120:
                        cands.append({"i": i, "signal": signal, "images": images_count})
            return meta, ps, cands

        metadata, pages, ocr_candidates = await asyncio.to_thread(_extract_base)

        ocr_targets: List[int] = []
        if enable_ocr and ocr_candidates:
            ocr_candidates.sort(key=lambda x: (x.get("signal", 0), -(x.get("images", 0))))
            for item in ocr_candidates[: int(max_ocr_pages)]:
                ocr_targets.append(int(item["i"]))

        if enable_ocr and ocr_targets:
            from ai_assistant.services.qwen_ocr_service import qwen_ocr_image_bytes
            try:
                from users.ai_config import resolve_ocr_params
            except Exception:
                resolve_ocr_params = None
            params = None
            try:
                if user is not None and resolve_ocr_params is not None:
                    params = resolve_ocr_params(user)
            except Exception:
                params = None
            sem = asyncio.Semaphore(2)

            async def _ocr_one(page_index: int) -> None:
                async with sem:
                    pages[page_index]["ocr_attempted"] = True
                    img_bytes = await asyncio.to_thread(_render_page_png_bytes, pdf_path, page_index)
                    if not img_bytes:
                        pages[page_index]["ocr_warnings"] = ["render_unavailable"]
                        return
                    try:
                        data = await asyncio.to_thread(
                            qwen_ocr_image_bytes,
                            img_bytes,
                            "image/png",
                            model=(getattr(params, "model", None) if params else None),
                            api_key=(getattr(params, "api_key", None) if params else None),
                            base_url=(getattr(params, "base_url", None) if params else None),
                        )
                    except Exception as e:
                        pages[page_index]["ocr_warnings"] = [f"ocr_error:{str(e)}"]
                        return
                    pages[page_index]["ocr_text"] = _clean(str(data.get("text") or ""))
                    pages[page_index]["ocr_confidence"] = str(data.get("confidence") or "")
                    pages[page_index]["ocr_warnings"] = data.get("warnings") or []

            await asyncio.gather(*[_ocr_one(i) for i in ocr_targets])

        blocks: List[str] = []
        blocks.append("【PDF结构化提取】")
        blocks.append(f"- ocr_enabled: {bool(enable_ocr)}")
        blocks.append(f"- pdfium_available: {bool(pdfium is not None)}")
        if metadata:
            blocks.append("【元数据】")
            for k in ("title", "author", "subject", "creator", "pages"):
                v = metadata.get(k, "")
                if v:
                    blocks.append(f"- {k}: {v}")
        for p in pages:
            blocks.append(f"\n【P{p['page']}】")
            if p.get("text"):
                blocks.append("TEXT:")
                blocks.append(p["text"][:2200])
            if p.get("tables"):
                blocks.append("TABLES:")
                blocks.append(p["tables"][:2200])
            if p.get("ocr_text"):
                blocks.append("OCR:")
                blocks.append(p["ocr_text"][:2200])

        llm_material = "\n".join(blocks).strip()
        if len(llm_material) > 50000:
            llm_material = llm_material[:50000] + "\n...(内容过长，已截断)"

        return {
            "metadata": metadata,
            "pages": pages,
            "llm_material": llm_material,
            "extracted_at": datetime.now().isoformat(),
        }
    
    def _extract_with_pypdf2(self, pdf_path: str) -> str:
        """
        使用PyPDF2作为备用方法提取PDF文本
        """
        text_content = ""
        try:
            with open(pdf_path, 'rb') as file:
                pdf_reader = PyPDF2.PdfReader(file)
                for page_num, page in enumerate(pdf_reader.pages, 1):
                    page_text = page.extract_text()
                    if page_text:
                        text_content += f"\n--- 第 {page_num} 页 ---\n"
                        text_content += page_text
                        text_content += "\n"
        except Exception as e:
            print(f"PyPDF2提取失败: {str(e)}")
        
        return text_content
    
    def analyze_requirements_structure(self, text: str) -> Dict[str, Any]:
        """
        分析需求文档的结构，提取关键信息
        """
        analysis = {
            'sections': [],
            'requirements': [],
            'functional_requirements': [],
            'non_functional_requirements': [],
            'use_cases': [],
            'business_rules': [],
            'data_requirements': []
        }
        
        # 分割文本为行
        lines = text.split('\n')
        current_section = None
        current_content = []
        
        # 常见的需求文档关键词模式
        section_patterns = {
            'functional': r'(功能需求|功能性需求|functional\s+requirement)',
            'non_functional': r'(非功能需求|非功能性需求|non.?functional\s+requirement)',
            'use_case': r'(用例|使用场景|use\s+case)',
            'business_rule': r'(业务规则|business\s+rule)',
            'data': r'(数据需求|数据结构|data\s+requirement)'
        }
        
        requirement_patterns = [
            r'需求\s*[：:]\s*(.+)',
            r'要求\s*[：:]\s*(.+)',
            r'应该\s*(.+)',
            r'必须\s*(.+)',
            r'系统\s*(.+)',
            r'用户\s*(.+)',
            r'REQ[-_]\d+\s*[：:]\s*(.+)'
        ]
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # 检测章节标题
            if self._is_section_header(line):
                if current_section and current_content:
                    analysis['sections'].append({
                        'title': current_section,
                        'content': '\n'.join(current_content)
                    })
                current_section = line
                current_content = []
                continue
            
            # 检测需求条目
            for pattern in requirement_patterns:
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    requirement = {
                        'id': f"REQ-{len(analysis['requirements']) + 1}",
                        'text': line,
                        'extracted_content': match.group(1) if match.groups() else line,
                        'section': current_section or '未分类'
                    }
                    analysis['requirements'].append(requirement)
                    
                    # 根据上下文分类需求
                    self._categorize_requirement(requirement, analysis, section_patterns)
                    break
            
            if current_section:
                current_content.append(line)
        
        # 添加最后一个章节
        if current_section and current_content:
            analysis['sections'].append({
                'title': current_section,
                'content': '\n'.join(current_content)
            })
        
        return analysis
    
    def _is_section_header(self, line: str) -> bool:
        """
        判断是否为章节标题
        """
        # 检测常见的标题格式
        patterns = [
            r'^\d+\.?\s+.+',  # 1. 标题 或 1 标题
            r'^第\s*[一二三四五六七八九十\d]+\s*[章节部分]\s*.+',  # 第一章 标题
            r'^[一二三四五六七八九十]+[、．.]\s*.+',  # 一、标题
            r'^[A-Z]+\.?\s+.+',  # A. 标题
            r'^\s*#+\s+.+',  # Markdown标题
        ]
        
        for pattern in patterns:
            if re.match(pattern, line):
                return True
        
        return False
    
    def _categorize_requirement(self, requirement: Dict, analysis: Dict, section_patterns: Dict):
        """
        根据内容将需求分类
        """
        text = requirement['text'].lower()
        section = requirement.get('section', '').lower()
        
        # 根据章节和内容分类
        if re.search(section_patterns['functional'], section + ' ' + text, re.IGNORECASE):
            analysis['functional_requirements'].append(requirement)
        elif re.search(section_patterns['non_functional'], section + ' ' + text, re.IGNORECASE):
            analysis['non_functional_requirements'].append(requirement)
        elif re.search(section_patterns['use_case'], section + ' ' + text, re.IGNORECASE):
            analysis['use_cases'].append(requirement)
        elif re.search(section_patterns['business_rule'], section + ' ' + text, re.IGNORECASE):
            analysis['business_rules'].append(requirement)
        elif re.search(section_patterns['data'], section + ' ' + text, re.IGNORECASE):
            analysis['data_requirements'].append(requirement)

pdf_service = PDFService()
