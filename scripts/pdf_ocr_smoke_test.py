from __future__ import annotations

import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.ingestion.parser import parse_document


EXPECTED_PHRASE = "ANNUAL LEAVE IS 15 DAYS"


def build_scanned_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (1654, 2339), "white")
    draw = ImageDraw.Draw(image)
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    font = ImageFont.truetype(str(font_path), 64)
    title_font = ImageFont.truetype(str(font_path), 76)
    draw.text((140, 240), "ENTERPRISE KNOWLEDGE POLICY", fill="black", font=title_font)
    draw.text((140, 520), EXPECTED_PHRASE, fill="black", font=font)
    draw.text((140, 680), "DOCUMENT VERSION 2026", fill="black", font=font)
    image.save(path, "PDF", resolution=150.0)


def main() -> None:
    os.environ.setdefault("KNOWLEDGE_PDF_PARSER", "docling")
    os.environ.setdefault("KNOWLEDGE_PDF_OCR", "1")
    os.environ.setdefault("KNOWLEDGE_PDF_OCR_ENGINE", "rapidocr")
    os.environ.setdefault("KNOWLEDGE_PDF_TABLE_STRUCTURE", "1")
    os.environ.setdefault("KNOWLEDGE_PDF_ALLOW_PYPDF_FALLBACK", "0")

    pdf_path = Path(".codex-tmp/smoke/scanned-policy.pdf").resolve()
    build_scanned_pdf(pdf_path)
    blocks = parse_document(
        pdf_path.name,
        pdf_path.read_bytes(),
        source_path=pdf_path,
    )
    text = "\n".join(block.text for block in blocks)
    normalized = " ".join(text.upper().split())
    if EXPECTED_PHRASE not in normalized:
        raise RuntimeError(f"OCR did not recover the expected phrase: {text!r}")
    if any(block.page != 1 for block in blocks):
        raise RuntimeError("OCR output contains an unexpected page number")
    if any(block.metadata.get("parser") != "docling" for block in blocks):
        raise RuntimeError("OCR output is missing Docling parser metadata")

    print(
        {
            "status": "ok",
            "parser": "docling",
            "ocr_engine": "rapidocr",
            "block_count": len(blocks),
            "pages": sorted({block.page for block in blocks}),
            "expected_phrase_found": True,
        }
    )


if __name__ == "__main__":
    main()
