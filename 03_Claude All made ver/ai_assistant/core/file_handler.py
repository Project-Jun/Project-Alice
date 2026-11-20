"""
파일 읽기 및 요약 모듈
지원 형식: PDF, DOCX, TXT, XLSX, CSV, 이미지(OCR)
"""
import os
from pathlib import Path


class FileHandler:
    """다양한 파일 형식을 텍스트로 변환"""

    SUPPORTED_EXTENSIONS = {
        ".pdf": "PDF 문서",
        ".docx": "Word 문서",
        ".doc": "Word 문서 (구버전)",
        ".txt": "텍스트 파일",
        ".md": "마크다운",
        ".xlsx": "Excel 스프레드시트",
        ".xls": "Excel 스프레드시트 (구버전)",
        ".csv": "CSV 파일",
        ".pptx": "PowerPoint",
        ".hwp": "한글 파일",
    }

    def read_file(self, file_path: str) -> str:
        """파일을 읽어 텍스트 반환"""
        path = Path(file_path)
        ext = path.suffix.lower()

        if not path.exists():
            raise FileNotFoundError(f"파일을 찾을 수 없습니다: {file_path}")

        readers = {
            ".pdf": self._read_pdf,
            ".docx": self._read_docx,
            ".doc": self._read_docx,
            ".txt": self._read_txt,
            ".md": self._read_txt,
            ".xlsx": self._read_excel,
            ".xls": self._read_excel,
            ".csv": self._read_csv,
            ".pptx": self._read_pptx,
        }

        reader = readers.get(ext)
        if reader is None:
            raise ValueError(f"지원하지 않는 파일 형식입니다: {ext}")

        return reader(file_path)

    def _read_pdf(self, path: str) -> str:
        try:
            import pdfplumber
            text_parts = []
            with pdfplumber.open(path) as pdf:
                for i, page in enumerate(pdf.pages):
                    text = page.extract_text()
                    if text:
                        text_parts.append(f"[페이지 {i+1}]\n{text}")
                    # 표 추출
                    tables = page.extract_tables()
                    for table in tables:
                        rows = [" | ".join(str(c) for c in row if c) for row in table if any(row)]
                        text_parts.append("\n".join(rows))
            return "\n\n".join(text_parts)
        except ImportError:
            raise ImportError("PDF 읽기를 위해 'pip install pdfplumber'가 필요합니다.")

    def _read_docx(self, path: str) -> str:
        try:
            from docx import Document
            doc = Document(path)
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            return "\n".join(paragraphs)
        except ImportError:
            raise ImportError("DOCX 읽기를 위해 'pip install python-docx'가 필요합니다.")

    def _read_txt(self, path: str) -> str:
        for encoding in ["utf-8", "cp949", "euc-kr", "latin-1"]:
            try:
                with open(path, "r", encoding=encoding) as f:
                    return f.read()
            except UnicodeDecodeError:
                continue
        raise ValueError("파일 인코딩을 읽을 수 없습니다.")

    def _read_excel(self, path: str) -> str:
        try:
            import pandas as pd
            xl = pd.ExcelFile(path)
            result = []
            for sheet in xl.sheet_names:
                df = pd.read_excel(path, sheet_name=sheet)
                result.append(f"[시트: {sheet}]\n{df.to_string()}")
            return "\n\n".join(result)
        except ImportError:
            raise ImportError("Excel 읽기를 위해 'pip install pandas openpyxl'이 필요합니다.")

    def _read_csv(self, path: str) -> str:
        try:
            import pandas as pd
            df = pd.read_csv(path, encoding="utf-8-sig")
            return df.to_string()
        except Exception:
            return self._read_txt(path)

    def _read_pptx(self, path: str) -> str:
        try:
            from pptx import Presentation
            prs = Presentation(path)
            slides_text = []
            for i, slide in enumerate(prs.slides):
                texts = []
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text.strip():
                        texts.append(shape.text)
                slides_text.append(f"[슬라이드 {i+1}]\n" + "\n".join(texts))
            return "\n\n".join(slides_text)
        except ImportError:
            raise ImportError("PPTX 읽기를 위해 'pip install python-pptx'가 필요합니다.")

    def build_summary_prompt(self, file_path: str, file_content: str) -> str:
        """파일 요약 요청 프롬프트 생성"""
        filename = Path(file_path).name
        # 컨텍스트 길이 제한 (약 100,000자)
        if len(file_content) > 100000:
            file_content = file_content[:100000] + "\n\n[... 내용이 너무 길어 일부 생략 ...]"

        return (
            f"다음은 '{filename}' 파일의 내용입니다. "
            f"핵심 내용을 한국어로 요약해주세요. "
            f"주요 포인트, 결론, 중요한 데이터를 포함해주세요.\n\n"
            f"=== 파일 내용 ===\n{file_content}"
        )
