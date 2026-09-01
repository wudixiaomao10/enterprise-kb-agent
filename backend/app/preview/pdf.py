from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class PDFPreviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class PDFHighlight:
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class RenderedPDFPage:
    png_bytes: bytes
    page_number: int
    page_count: int
    width: int
    height: int


@dataclass(frozen=True)
class PDFPreviewLocation:
    page_number: int
    page_count: int
    page_width: float
    page_height: float
    highlights: tuple[PDFHighlight, ...]
    match_method: str


def render_pdf_page(
    raw_bytes: bytes,
    page_number: int,
    *,
    dpi: int = 144,
) -> RenderedPDFPage:
    pymupdf = require_pymupdf()
    document = open_pdf(pymupdf, raw_bytes)
    try:
        page = require_page(document, page_number)
        scale = dpi / 72
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        return RenderedPDFPage(
            png_bytes=pixmap.tobytes("png"),
            page_number=page_number,
            page_count=document.page_count,
            width=pixmap.width,
            height=pixmap.height,
        )
    finally:
        document.close()


def locate_pdf_chunk(
    raw_bytes: bytes,
    page_number: int,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> PDFPreviewLocation:
    pymupdf = require_pymupdf()
    document = open_pdf(pymupdf, raw_bytes)
    try:
        page = require_page(document, page_number)
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)
        stored = stored_highlights(metadata or {})
        if stored:
            highlights = stored
            method = "docling-bbox"
        else:
            highlights = search_page_highlights(pymupdf, page, content)
            method = "text-search" if highlights else "page-only"
        return PDFPreviewLocation(
            page_number=page_number,
            page_count=document.page_count,
            page_width=page_width,
            page_height=page_height,
            highlights=tuple(highlights),
            match_method=method,
        )
    finally:
        document.close()


def require_pymupdf():
    try:
        import pymupdf
    except ImportError as error:  # pragma: no cover - environment guard
        raise PDFPreviewError(
            "PyMuPDF is required for PDF page preview. Install requirements.txt."
        ) from error
    return pymupdf


def open_pdf(pymupdf, raw_bytes: bytes):
    try:
        return pymupdf.open(stream=raw_bytes, filetype="pdf")
    except Exception as error:
        raise PDFPreviewError(f"Unable to open PDF: {error}") from error


def require_page(document, page_number: int):
    if page_number < 1 or page_number > document.page_count:
        raise PDFPreviewError(
            f"PDF page {page_number} is outside 1..{document.page_count}"
        )
    return document.load_page(page_number - 1)


def stored_highlights(metadata: dict[str, Any]) -> list[PDFHighlight]:
    raw_boxes = metadata.get("bbox_norms")
    if not isinstance(raw_boxes, list):
        single = metadata.get("bbox_norm")
        raw_boxes = [single] if isinstance(single, list) else []
    highlights: list[PDFHighlight] = []
    for raw in raw_boxes:
        if not isinstance(raw, (list, tuple)) or len(raw) != 4:
            continue
        try:
            left, top, right, bottom = (float(value) for value in raw)
        except (TypeError, ValueError):
            continue
        left, top = clamp(left), clamp(top)
        right, bottom = clamp(right), clamp(bottom)
        if right <= left or bottom <= top:
            continue
        highlights.append(
            PDFHighlight(left, top, right - left, bottom - top)
        )
    return highlights


def search_page_highlights(pymupdf, page, content: str) -> list[PDFHighlight]:
    flags = pymupdf.TEXT_DEHYPHENATE | pymupdf.TEXT_PRESERVE_LIGATURES
    rectangles = []
    for candidate in search_candidates(content):
        try:
            matches = page.search_for(candidate, flags=flags)
        except Exception:
            matches = []
        if not matches:
            continue
        rectangles.extend(matches)
        if len(rectangles) >= 12:
            break
    seen: set[tuple[float, float, float, float]] = set()
    highlights: list[PDFHighlight] = []
    width = max(float(page.rect.width), 1.0)
    height = max(float(page.rect.height), 1.0)
    for rectangle in rectangles[:12]:
        normalized = (
            clamp(float(rectangle.x0) / width),
            clamp(float(rectangle.y0) / height),
            clamp(float(rectangle.x1) / width),
            clamp(float(rectangle.y1) / height),
        )
        key = tuple(round(value, 5) for value in normalized)
        if key in seen:
            continue
        seen.add(key)
        left, top, right, bottom = normalized
        if right > left and bottom > top:
            highlights.append(
                PDFHighlight(left, top, right - left, bottom - top)
            )
    return highlights


def search_candidates(content: str) -> list[str]:
    cleaned = re.sub(r"(?m)^#{1,6}\s+", "", content or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return []
    candidates: list[str] = []
    sentences = re.split(r"(?<=[。！？.!?])\s*", cleaned)
    for sentence in sentences:
        sentence = sentence.strip(" |")
        if len(sentence) >= 8:
            candidates.extend(split_candidate(sentence, 160))
    if not candidates:
        candidates.extend(split_candidate(cleaned, 160))
    unique: list[str] = []
    for candidate in sorted(candidates, key=len, reverse=True):
        if candidate and candidate not in unique:
            unique.append(candidate)
    return unique[:8]


def split_candidate(text: str, maximum: int) -> list[str]:
    if len(text) <= maximum:
        return [text]
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + maximum, len(text))
        if end < len(text):
            boundary = text.rfind(" ", start, end)
            if boundary > start + maximum // 2:
                end = boundary
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        start = max(end, start + 1)
    return pieces


def clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
