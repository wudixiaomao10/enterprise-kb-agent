from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import uuid
import zipfile
from contextlib import redirect_stderr
from dataclasses import dataclass
from io import BytesIO, StringIO
from pathlib import Path, PurePosixPath
from typing import Any


TEXT_EXTENSIONS = frozenset({".md", ".markdown", ".txt"})
ARCHIVE_EXTENSIONS = frozenset({".docx"})
UPLOAD_EXTENSIONS = TEXT_EXTENSIONS | ARCHIVE_EXTENSIONS | {".pdf"}

_PROMPT_INJECTION_PATTERNS = (
    ("instruction_override", re.compile(
        r"(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|above|system|developer)\s+(?:instructions?|messages?)",
        re.IGNORECASE,
    )),
    ("instruction_override_zh", re.compile(
        r"(?:忽略|无视|忘记)(?:之前|上文|系统|开发者|所有)?(?:的)?(?:指令|提示|规则|消息)",
    )),
    ("prompt_disclosure", re.compile(
        r"(?:reveal|print|show|leak|dump)\s+(?:the\s+)?(?:system\s+prompt|developer\s+message|secret|token|password|api\s*key)",
        re.IGNORECASE,
    )),
    ("prompt_disclosure_zh", re.compile(
        r"(?:泄露|输出|打印|显示|告诉我)(?:系统提示词|开发者消息|密钥|令牌|密码|完整提示)",
    )),
    ("role_hijack", re.compile(
        r"(?:you\s+are\s+now|act\s+as|system\s*message\s*:|developer\s*message\s*:)",
        re.IGNORECASE,
    )),
    ("policy_bypass", re.compile(
        r"(?:do\s+not\s+(?:cite|verify)|bypass\s+(?:access|permission|acl)|不要(?:引用|验证)|绕过(?:权限|访问控制))",
        re.IGNORECASE,
    )),
)


class UploadSecurityError(ValueError):
    def __init__(self, code: str, message: str, *, status: str = "rejected") -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class UploadSecurityReport:
    filename: str
    size_bytes: int
    status: str
    virus_scan_status: str


def detect_prompt_injection(text: str) -> tuple[str, ...]:
    signals = [name for name, pattern in _PROMPT_INJECTION_PATTERNS if pattern.search(text)]
    return tuple(signals)


def contains_prompt_injection(text: str) -> bool:
    return bool(detect_prompt_injection(text))


def validate_upload(filename: str, raw_bytes: bytes) -> UploadSecurityReport:
    safe_name = Path(str(filename)).name
    extension = Path(safe_name).suffix.lower()
    if not safe_name or safe_name in {".", ".."}:
        raise UploadSecurityError("invalid_filename", "Invalid filename")
    if extension not in UPLOAD_EXTENSIONS:
        raise UploadSecurityError(
            "unsupported_file_type",
            "Only PDF, DOCX, Markdown, and TXT files are accepted",
        )

    max_bytes = upload_max_bytes()
    if len(raw_bytes) > max_bytes:
        raise UploadSecurityError(
            "file_too_large",
            f"Document exceeds upload limit of {max_bytes} bytes",
        )
    if extension == ".pdf":
        validate_pdf(raw_bytes)
    elif extension in ARCHIVE_EXTENSIONS:
        validate_docx_archive(raw_bytes)
    else:
        validate_text(raw_bytes)

    virus_scan_status = run_virus_scan(safe_name, raw_bytes)
    return UploadSecurityReport(
        filename=safe_name,
        size_bytes=len(raw_bytes),
        status="clean",
        virus_scan_status=virus_scan_status,
    )


def upload_max_bytes() -> int:
    return env_int("KNOWLEDGE_UPLOAD_MAX_BYTES", 100 * 1024 * 1024)


def quarantine_upload(filename: str, raw_bytes: bytes, reason: str) -> Path:
    root = Path(
        os.getenv("KNOWLEDGE_QUARANTINE_DIR", ".codex-tmp/quarantine")
    ).resolve()
    root.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(str(filename)).name)
    safe_name = safe_name or "upload.bin"
    target = root / f"{uuid.uuid4().hex}_{safe_name}.quarantined"
    target.write_bytes(raw_bytes)
    metadata = target.with_suffix(target.suffix + ".json")
    metadata.write_text(
        json.dumps(
            {"reason": str(reason), "status": "quarantined"},
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    return target


def validate_pdf(raw_bytes: bytes) -> None:
    if not raw_bytes.startswith(b"%PDF-"):
        raise UploadSecurityError(
            "file_type_mismatch",
            "PDF content does not have a valid PDF signature",
        )
    if re.search(rb"/(?:JavaScript|JS|Launch|EmbeddedFile|OpenAction|AA)\b", raw_bytes):
        raise UploadSecurityError(
            "pdf_active_content",
            "PDF active content is not accepted",
        )
    try:
        from pypdf import PdfReader

        # pypdf may emit parser warnings for hostile input; keep the API response clean.
        with redirect_stderr(StringIO()):
            reader = PdfReader(BytesIO(raw_bytes), strict=False)
        if reader.is_encrypted:
            raise UploadSecurityError(
                "encrypted_pdf",
                "Encrypted PDFs are not accepted for indexing",
            )
        max_pages = env_int("KNOWLEDGE_PDF_MAX_PAGES", 500)
        if len(reader.pages) > max_pages:
            raise UploadSecurityError(
                "pdf_page_limit",
                f"PDF exceeds the maximum of {max_pages} pages",
            )
    except UploadSecurityError:
        raise
    except Exception as error:
        raise UploadSecurityError(
            "invalid_pdf",
            "PDF structure validation failed",
        ) from error


def validate_docx_archive(raw_bytes: bytes) -> None:
    try:
        with zipfile.ZipFile(BytesIO(raw_bytes)) as archive:
            infos = archive.infolist()
            validate_archive_entries(infos)
            names = {info.filename for info in infos}
            required = {"[Content_Types].xml", "word/document.xml"}
            if not required.issubset(names):
                raise UploadSecurityError(
                    "file_type_mismatch",
                    "DOCX archive is missing required OOXML parts",
                )
            if any(name.lower().endswith("vbaproject.bin") for name in names):
                raise UploadSecurityError(
                    "active_content",
                    "Macro-enabled documents are not accepted",
                )
    except UploadSecurityError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise UploadSecurityError(
            "invalid_docx",
            "DOCX archive validation failed",
        ) from error


def validate_archive_entries(infos: list[zipfile.ZipInfo]) -> None:
    max_entries = env_int("KNOWLEDGE_UPLOAD_MAX_ARCHIVE_ENTRIES", 1000)
    max_uncompressed = env_int(
        "KNOWLEDGE_UPLOAD_MAX_ARCHIVE_BYTES", 200 * 1024 * 1024
    )
    max_ratio = env_int("KNOWLEDGE_UPLOAD_MAX_COMPRESSION_RATIO", 200)
    if len(infos) > max_entries:
        raise UploadSecurityError(
            "archive_entry_limit",
            f"Archive exceeds the maximum of {max_entries} entries",
        )

    total_size = 0
    for info in infos:
        name = info.filename.replace("\\", "/")
        parts = PurePosixPath(name).parts
        if PurePosixPath(name).is_absolute() or ".." in parts:
            raise UploadSecurityError(
                "archive_path_traversal",
                "Archive contains an unsafe path",
            )
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise UploadSecurityError(
                "archive_symlink",
                "Archive symlinks are not accepted",
            )
        total_size += info.file_size
        if total_size > max_uncompressed:
            raise UploadSecurityError(
                "archive_size_limit",
                f"Archive exceeds the maximum of {max_uncompressed} uncompressed bytes",
            )
        if info.file_size and info.file_size / max(info.compress_size, 1) > max_ratio:
            raise UploadSecurityError(
                "archive_compression_ratio",
                "Archive compression ratio exceeds the configured limit",
            )


def validate_text(raw_bytes: bytes) -> None:
    if b"\x00" in raw_bytes:
        raise UploadSecurityError(
            "binary_text_mismatch",
            "Text files must not contain binary NUL bytes",
        )
    for signature in (b"%PDF-", b"PK\x03\x04", b"MZ", b"\x7fELF"):
        if raw_bytes.startswith(signature):
            raise UploadSecurityError(
                "file_type_mismatch",
                "Text filename does not match the file content",
            )
    try:
        raw_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UploadSecurityError(
            "invalid_text_encoding",
            "Text files must be valid UTF-8",
        ) from error


def run_virus_scan(filename: str, raw_bytes: bytes) -> str:
    command = os.getenv("KNOWLEDGE_VIRUS_SCANNER_COMMAND", "").strip()
    required = env_bool("KNOWLEDGE_VIRUS_SCAN_REQUIRED", False)
    if not command:
        if required:
            raise UploadSecurityError(
                "virus_scan_required",
                "A configured virus scanner is required before indexing",
                status="quarantined",
            )
        return "not_configured"

    try:
        args = shlex.split(command)
        if not args:
            raise ValueError("empty scanner command")
        timeout = env_int("KNOWLEDGE_VIRUS_SCAN_TIMEOUT_SECONDS", 30)
        completed = subprocess.run(
            [*args, "-"],
            input=raw_bytes,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise UploadSecurityError(
            "virus_scan_unavailable",
            f"Virus scanner did not complete for {filename}",
            status="quarantined",
        ) from error
    if completed.returncode != 0:
        raise UploadSecurityError(
            "malware_detected",
            f"Virus scanner rejected {filename}",
            status="quarantined",
        )
    return "clean"


def env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise UploadSecurityError("invalid_security_config", f"{name} must be an integer") from error
    if value <= 0:
        raise UploadSecurityError("invalid_security_config", f"{name} must be positive")
    return value


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "UploadSecurityError",
    "UploadSecurityReport",
    "contains_prompt_injection",
    "detect_prompt_injection",
    "quarantine_upload",
    "run_virus_scan",
    "upload_max_bytes",
    "validate_upload",
]
