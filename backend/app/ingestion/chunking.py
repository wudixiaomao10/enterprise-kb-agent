from __future__ import annotations

import hashlib

from backend.app.models.knowledge import ACLEntry, DocumentChunk, ParsedBlock
from backend.app.retrieval.providers import EmbeddingProvider, LocalHashEmbeddingProvider


def build_chunks(
    *,
    document_id: str,
    version_id: str,
    blocks: list[ParsedBlock],
    acl: list[ACLEntry],
    embedding_provider: EmbeddingProvider | None = None,
    chunk_size: int = 900,
    overlap: int = 120,
) -> list[DocumentChunk]:
    provider = embedding_provider or LocalHashEmbeddingProvider()
    chunks: list[DocumentChunk] = []
    for block in blocks:
        split_chunks = split_text(block.text, chunk_size, overlap)
        embeddings = provider.embed_texts([text for _, text in split_chunks])
        for (offset, text), embedding in zip(split_chunks, embeddings):
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            chunk_id = stable_chunk_id(document_id, version_id, block.page, offset, text)
            chunks.append(
                DocumentChunk(
                    chunk_id=chunk_id,
                    document_id=document_id,
                    version_id=version_id,
                    page=block.page,
                    section_path=block.section_path,
                    content=text,
                    content_hash=content_hash,
                    acl=acl,
                    embedding=embedding,
                    metadata={
                        **block.metadata,
                        "offset": offset,
                        "embedding_provider": provider.name,
                        "embedding_dimensions": provider.dimensions,
                    },
                )
            )
    return chunks


def split_text(text: str, chunk_size: int, overlap: int) -> list[tuple[int, str]]:
    chunks: list[tuple[int, str]] = []
    start = 0
    text_length = len(text)
    while start < text_length:
        end = min(start + chunk_size, text_length)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append((start, chunk))
        if end == text_length:
            break
        start = max(end - overlap, start + 1)
    return chunks


def stable_chunk_id(
    document_id: str,
    version_id: str,
    page: int,
    offset: int,
    text: str,
) -> str:
    raw = f"{document_id}:{version_id}:{page}:{offset}:{text}".encode("utf-8")
    return f"chk_{hashlib.sha256(raw).hexdigest()[:16]}"
