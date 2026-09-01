from __future__ import annotations

import mimetypes
import os
import tempfile
import uuid
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Protocol
from urllib.parse import unquote, urlparse

from backend.app.retrieval.providers import load_dotenv_if_available


class ObjectStorageError(RuntimeError):
    pass


class ObjectStorageNotFound(ObjectStorageError):
    pass


class ObjectStorage(Protocol):
    name: str

    def put(self, filename: str, raw_bytes: bytes) -> str:
        ...

    def read(self, storage_uri: str) -> bytes:
        ...

    def materialize(self, storage_uri: str) -> AbstractContextManager[Path]:
        ...

    def delete(self, storage_uri: str) -> None:
        ...


class LocalObjectStorage:
    name = "local-filesystem"

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def put(self, filename: str, raw_bytes: bytes) -> str:
        safe_name = safe_filename(filename)
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{uuid.uuid4().hex}_{safe_name}"
        temporary = target.with_suffix(target.suffix + ".uploading")
        temporary.write_bytes(raw_bytes)
        temporary.replace(target)
        return str(target)

    def read(self, storage_uri: str) -> bytes:
        path = self._resolve(storage_uri)
        try:
            return path.read_bytes()
        except FileNotFoundError as error:
            raise ObjectStorageNotFound(storage_uri) from error

    @contextmanager
    def materialize(self, storage_uri: str) -> Iterator[Path]:
        path = self._resolve(storage_uri)
        if not path.exists():
            raise ObjectStorageNotFound(storage_uri)
        yield path

    def delete(self, storage_uri: str) -> None:
        self._resolve(storage_uri).unlink(missing_ok=True)

    def _resolve(self, storage_uri: str) -> Path:
        direct_path = Path(storage_uri)
        if direct_path.is_absolute():
            raw_path = storage_uri
        else:
            parsed = urlparse(storage_uri)
            if parsed.scheme not in {"", "file"}:
                raise ObjectStorageError(f"Unsupported local storage URI: {storage_uri}")
            raw_path = unquote(parsed.path) if parsed.scheme == "file" else storage_uri
        path = Path(raw_path).resolve()
        if not path.is_relative_to(self.root):
            raise ObjectStorageError("Local storage URI escapes the configured root")
        return path


class S3ObjectStorage:
    name = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        region: str = "us-east-1",
        access_key: str | None = None,
        secret_key: str | None = None,
        session_token: str | None = None,
        prefix: str = "documents",
        materialize_dir: Path = Path(".codex-tmp/materialized-documents"),
        auto_create_bucket: bool = False,
        force_path_style: bool = True,
        client=None,
    ) -> None:
        if not bucket.strip():
            raise ValueError("S3 bucket cannot be empty")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.materialize_dir = materialize_dir.resolve()
        self.region = region
        if client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as error:
                raise RuntimeError("Install boto3 to use S3 object storage") from error
            self.client = boto3.client(
                "s3",
                endpoint_url=endpoint_url or None,
                region_name=region,
                aws_access_key_id=access_key or None,
                aws_secret_access_key=secret_key or None,
                aws_session_token=session_token or None,
                config=Config(
                    signature_version="s3v4",
                    s3={
                        "addressing_style": "path" if force_path_style else "auto"
                    },
                ),
            )
        else:
            self.client = client
        if auto_create_bucket:
            self.ensure_bucket()

    def ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return
        except Exception as error:
            if not is_missing_bucket_error(error):
                raise ObjectStorageError(
                    f"Unable to access S3 bucket {self.bucket}: {error}"
                ) from error
        create_args = {"Bucket": self.bucket}
        if self.region != "us-east-1":
            create_args["CreateBucketConfiguration"] = {
                "LocationConstraint": self.region
            }
        self.client.create_bucket(**create_args)

    def put(self, filename: str, raw_bytes: bytes) -> str:
        safe_name = safe_filename(filename)
        now = datetime.now(timezone.utc)
        key_parts = [part for part in [self.prefix, now.strftime("%Y/%m")] if part]
        key_parts.append(f"{uuid.uuid4().hex}_{safe_name}")
        key = "/".join(key_parts)
        content_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        args = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": raw_bytes,
            "ContentLength": len(raw_bytes),
            "ContentType": content_type,
        }
        server_side_encryption = os.getenv("KNOWLEDGE_S3_SERVER_SIDE_ENCRYPTION", "")
        if server_side_encryption:
            args["ServerSideEncryption"] = server_side_encryption
        self.client.put_object(**args)
        return f"s3://{self.bucket}/{key}"

    def read(self, storage_uri: str) -> bytes:
        bucket, key = self._parse_uri(storage_uri)
        try:
            response = self.client.get_object(Bucket=bucket, Key=key)
            body = response["Body"]
            try:
                return body.read()
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
        except Exception as error:
            if is_missing_object_error(error):
                raise ObjectStorageNotFound(storage_uri) from error
            raise ObjectStorageError(f"Unable to read {storage_uri}: {error}") from error

    @contextmanager
    def materialize(self, storage_uri: str) -> Iterator[Path]:
        _, key = self._parse_uri(storage_uri)
        suffix = Path(key).suffix
        self.materialize_dir.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            prefix="knowledge-",
            suffix=suffix,
            dir=self.materialize_dir,
            delete=False,
        )
        path = Path(handle.name)
        try:
            with handle:
                handle.write(self.read(storage_uri))
            yield path
        finally:
            path.unlink(missing_ok=True)

    def delete(self, storage_uri: str) -> None:
        bucket, key = self._parse_uri(storage_uri)
        self.client.delete_object(Bucket=bucket, Key=key)

    def _parse_uri(self, storage_uri: str) -> tuple[str, str]:
        parsed = urlparse(storage_uri)
        if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
            raise ObjectStorageError(f"Invalid S3 storage URI: {storage_uri}")
        if parsed.netloc != self.bucket:
            raise ObjectStorageError(
                f"S3 URI bucket {parsed.netloc} does not match configured bucket"
            )
        return parsed.netloc, unquote(parsed.path.lstrip("/"))


class RoutingObjectStorage:
    def __init__(self, primary: ObjectStorage, local: LocalObjectStorage) -> None:
        self.primary = primary
        self.local = local
        self.name = primary.name

    def put(self, filename: str, raw_bytes: bytes) -> str:
        return self.primary.put(filename, raw_bytes)

    def read(self, storage_uri: str) -> bytes:
        return self._backend(storage_uri).read(storage_uri)

    def materialize(self, storage_uri: str) -> AbstractContextManager[Path]:
        return self._backend(storage_uri).materialize(storage_uri)

    def delete(self, storage_uri: str) -> None:
        self._backend(storage_uri).delete(storage_uri)

    def _backend(self, storage_uri: str) -> ObjectStorage:
        return self.primary if urlparse(storage_uri).scheme == "s3" else self.local


def create_object_storage(storage_dir: Path | None = None) -> ObjectStorage:
    load_dotenv_if_available()
    local = LocalObjectStorage(
        storage_dir
        or Path(os.getenv("KNOWLEDGE_STORAGE_DIR", ".codex-tmp/knowledge-documents"))
    )
    mode = os.getenv("KNOWLEDGE_OBJECT_STORAGE", "local").strip().lower()
    if mode == "local":
        return local
    if mode not in {"s3", "minio"}:
        raise RuntimeError("KNOWLEDGE_OBJECT_STORAGE must be local, s3, or minio")
    primary = S3ObjectStorage(
        bucket=require_env("KNOWLEDGE_S3_BUCKET"),
        endpoint_url=os.getenv("KNOWLEDGE_S3_ENDPOINT") or None,
        region=os.getenv("KNOWLEDGE_S3_REGION", "us-east-1"),
        access_key=os.getenv("KNOWLEDGE_S3_ACCESS_KEY") or None,
        secret_key=os.getenv("KNOWLEDGE_S3_SECRET_KEY") or None,
        session_token=os.getenv("KNOWLEDGE_S3_SESSION_TOKEN") or None,
        prefix=os.getenv("KNOWLEDGE_S3_PREFIX", "documents"),
        materialize_dir=Path(
            os.getenv(
                "KNOWLEDGE_MATERIALIZE_DIR", ".codex-tmp/materialized-documents"
            )
        ),
        auto_create_bucket=env_bool("KNOWLEDGE_S3_AUTO_CREATE_BUCKET", False),
        force_path_style=env_bool("KNOWLEDGE_S3_FORCE_PATH_STYLE", mode == "minio"),
    )
    return RoutingObjectStorage(primary, local)


def safe_filename(filename: str) -> str:
    safe_name = Path(filename).name
    if not safe_name or safe_name in {".", ".."}:
        raise ValueError("Invalid filename")
    return safe_name


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def error_code(error: Exception) -> str:
    response = getattr(error, "response", {}) or {}
    return str((response.get("Error") or {}).get("Code", ""))


def is_missing_bucket_error(error: Exception) -> bool:
    return error_code(error) in {"404", "NoSuchBucket", "NotFound"}


def is_missing_object_error(error: Exception) -> bool:
    return error_code(error) in {"404", "NoSuchKey", "NoSuchObject", "NotFound"}
