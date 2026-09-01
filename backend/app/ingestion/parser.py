from __future__ import annotations

import os
import re
import zipfile
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

from backend.app.models.knowledge import ParsedBlock, SourceType


class DocumentParseError(RuntimeError):
    pass


def detect_source_type(filename: str) -> SourceType:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return SourceType.PDF
    if suffix in {".docx", ".doc"}:
        return SourceType.WORD
    if suffix in {".md", ".markdown"}:
        return SourceType.MARKDOWN
    return SourceType.TEXT


def parse_document(
    filename: str,
    raw_bytes: bytes,
    *,
    source_path: Path | None = None,
) -> list[ParsedBlock]:
    source_type = detect_source_type(filename)
    if source_type == SourceType.PDF:
        blocks = parse_pdf_document(filename, raw_bytes, source_path=source_path)
    elif source_type == SourceType.WORD:
        blocks = parse_text_blocks(
            extract_docx_text(raw_bytes),
            source_type,
            metadata={"parser": "docx-xml", "ocr_used": False},
        )
    else:
        blocks = parse_text_blocks(
            raw_bytes.decode("utf-8", errors="replace"),
            source_type,
            metadata={"parser": "plain-text", "ocr_used": False},
        )
    if not blocks:
        raise DocumentParseError(f"No indexable content was extracted from {filename}")
    return blocks


def parse_pdf_document(
    filename: str,
    raw_bytes: bytes,
    *,
    source_path: Path | None,
) -> list[ParsedBlock]:
    parser = os.getenv("KNOWLEDGE_PDF_PARSER", "docling").strip().lower()
    if parser == "pypdf":
        return parse_pdf_with_pypdf(filename, raw_bytes)
    if parser != "docling":
        raise DocumentParseError(
            "Unsupported KNOWLEDGE_PDF_PARSER. Use docling or pypdf."
        )

    try:
        return parse_pdf_with_docling(filename, raw_bytes, source_path=source_path)
    except Exception as error:
        allow_fallback = os.getenv("KNOWLEDGE_PDF_ALLOW_PYPDF_FALLBACK", "0").lower()
        if allow_fallback not in {"1", "true", "yes"}:
            if isinstance(error, DocumentParseError):
                raise
            raise DocumentParseError(f"Docling failed for {filename}: {error}") from error
        return parse_pdf_with_pypdf(filename, raw_bytes)


def parse_pdf_with_docling(
    filename: str,
    raw_bytes: bytes,
    *,
    source_path: Path | None,
) -> list[ParsedBlock]:
    if source_path is None:
        raise DocumentParseError("Docling PDF parsing requires a persisted source_path")
    if not source_path.exists():
        raise DocumentParseError(f"PDF source does not exist: {source_path}")

    converter = get_docling_converter()
    max_pages = int(os.getenv("KNOWLEDGE_PDF_MAX_PAGES", "500"))
    max_file_size = int(os.getenv("KNOWLEDGE_PDF_MAX_FILE_BYTES", str(100 * 1024 * 1024)))
    result = converter.convert(
        source_path,
        raises_on_error=True,
        max_num_pages=max_pages,
        max_file_size=max_file_size,
    )
    document = result.document
    pages = getattr(document, "pages", {})
    page_numbers = sorted(int(page_no) for page_no in pages) or [1]
    metadata = {
        "parser": "docling",
        "ocr_enabled": env_bool("KNOWLEDGE_PDF_OCR", True),
        "ocr_engine": os.getenv("KNOWLEDGE_PDF_OCR_ENGINE", "rapidocr"),
        "table_structure": env_bool("KNOWLEDGE_PDF_TABLE_STRUCTURE", True),
    }
    blocks: list[ParsedBlock] = []
    for page_number in page_numbers:
        page_blocks = parse_docling_page_blocks(
            document,
            page_number,
            metadata,
        )
        if not page_blocks:
            markdown = document.export_to_markdown(
                page_no=page_number,
                strict_text=False,
                compact_tables=False,
            )
            page_blocks = parse_text_blocks(
                markdown,
                SourceType.PDF,
                page_number=page_number,
                metadata=metadata,
            )
        blocks.extend(page_blocks)
    validate_pdf_extraction(filename, blocks)
    return blocks


def parse_docling_page_blocks(
    document,
    page_number: int,
    base_metadata: dict,
) -> list[ParsedBlock]:
    page = document.pages.get(page_number)
    if page is None:
        return []
    current_section = "正文"
    blocks: list[ParsedBlock] = []
    for item, _ in document.iterate_items(page_no=page_number):
        label = getattr(getattr(item, "label", None), "value", "")
        text = docling_item_text(item, document)
        if label in {"title", "section_header"} and text:
            current_section = text.strip()
            continue
        cleaned = text.strip()
        if not cleaned:
            continue
        item_metadata = {
            **base_metadata,
            **docling_layout_metadata(item, page_number, page.size),
            "docling_label": label or "unknown",
        }
        blocks.append(
            ParsedBlock(
                text=cleaned,
                page=page_number,
                section_path=current_section,
                metadata=item_metadata,
            )
        )
    return blocks


def docling_item_text(item, document) -> str:
    if getattr(getattr(item, "label", None), "value", "") == "table":
        export = getattr(item, "export_to_markdown", None)
        if callable(export):
            try:
                return str(export(document))
            except Exception:
                pass
    return str(getattr(item, "text", "") or "")


def docling_layout_metadata(item, page_number: int, page_size) -> dict:
    boxes: list[list[float]] = []
    for provenance in getattr(item, "prov", []) or []:
        if int(provenance.page_no) != page_number:
            continue
        normalized = (
            provenance.bbox.to_top_left_origin(float(page_size.height))
            .normalized(page_size)
            .as_tuple()
        )
        boxes.append([round(float(value), 6) for value in normalized])
    if not boxes:
        return {}
    return {
        "bbox_norms": boxes,
        "layout_source": "docling-provenance",
        "page_width": float(page_size.width),
        "page_height": float(page_size.height),
    }


@lru_cache(maxsize=1)
def get_docling_converter():
    model_cache = Path(
        os.getenv("KNOWLEDGE_MODEL_CACHE_DIR", ".codex-tmp/model-cache")
    ).resolve()
    huggingface_cache = model_cache / "huggingface"
    huggingface_cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(huggingface_cache))

    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            OcrAutoOptions,
            PdfPipelineOptions,
            RapidOcrOptions,
            TesseractCliOcrOptions,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as error:
        raise DocumentParseError(
            "Docling is required for production PDF parsing. "
            "Install requirements.txt."
        ) from error

    options = PdfPipelineOptions()
    options.do_ocr = env_bool("KNOWLEDGE_PDF_OCR", True)
    options.do_table_structure = env_bool("KNOWLEDGE_PDF_TABLE_STRUCTURE", True)
    ocr_engine = os.getenv("KNOWLEDGE_PDF_OCR_ENGINE", "rapidocr").strip().lower()
    if ocr_engine == "rapidocr":
        options.ocr_options = RapidOcrOptions()
    elif ocr_engine == "tesseract":
        options.ocr_options = TesseractCliOcrOptions()
    elif ocr_engine == "auto":
        options.ocr_options = OcrAutoOptions()
    else:
        raise DocumentParseError(
            "Unsupported KNOWLEDGE_PDF_OCR_ENGINE. Use rapidocr, tesseract, or auto."
        )
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=options),
        },
    )


def parse_pdf_with_pypdf(filename: str, raw_bytes: bytes) -> list[ParsedBlock]:
    try:
        from pypdf import PdfReader
    except ImportError as error:
        raise DocumentParseError("pypdf is not installed") from error

    try:
        reader = PdfReader(BytesIO(raw_bytes), strict=False)
    except Exception as error:
        raise DocumentParseError(f"Invalid PDF {filename}: {error}") from error
    blocks: list[ParsedBlock] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        blocks.extend(
            parse_text_blocks(
                text,
                SourceType.PDF,
                page_number=page_number,
                metadata={
                    "parser": "pypdf",
                    "ocr_enabled": False,
                    "ocr_used": False,
                    "table_structure": False,
                },
            )
        )
    validate_pdf_extraction(filename, blocks)
    return blocks


def validate_pdf_extraction(filename: str, blocks: list[ParsedBlock]) -> None:
    extracted_chars = sum(len(block.text.strip()) for block in blocks)
    minimum = int(os.getenv("KNOWLEDGE_PDF_MIN_TEXT_CHARS", "20"))
    if extracted_chars < minimum:
        raise DocumentParseError(
            f"PDF extraction quality check failed for {filename}: "
            f"{extracted_chars} characters, minimum {minimum}"
        )


def extract_docx_text(raw_bytes: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(raw_bytes)) as archive:
            xml = archive.read("word/document.xml")
    except Exception:
        return raw_bytes.decode("utf-8", errors="replace")

    root = ElementTree.fromstring(xml)
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        texts = [node.text or "" for node in paragraph.findall(".//w:t", namespace)]
        merged = "".join(texts).strip()
        if merged:
            paragraphs.append(merged)
    return "\n\n".join(paragraphs)


def parse_text_blocks(
    text: str,
    source_type: SourceType,
    *,
    page_number: int | None = None,
    metadata: dict | None = None,
) -> list[ParsedBlock]:
    pages = [text] if page_number is not None else text.split("\f")
    blocks: list[ParsedBlock] = []
    current_section = "正文"
    for offset, page_text in enumerate(pages, start=1):
        actual_page = page_number if page_number is not None else offset
        parts = split_by_markdown_headings(page_text)
        for heading, body in parts:
            if heading:
                current_section = heading
            cleaned = body.strip()
            if cleaned:
                blocks.append(
                    ParsedBlock(
                        text=cleaned,
                        page=actual_page,
                        section_path=current_section,
                        metadata=dict(metadata or {}),
                    )
                )
    return blocks


def split_by_markdown_headings(text: str) -> list[tuple[str | None, str]]:
    matches = list(re.finditer(r"^(#{1,6})\s+(.+?)\s*$", text, re.MULTILINE))
    if not matches:
        return [(None, text)]

    parts: list[tuple[str | None, str]] = []
    first = matches[0]
    if first.start() > 0:
        parts.append((None, text[: first.start()]))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        parts.append((match.group(2).strip(), text[start:end]))
    return parts


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
