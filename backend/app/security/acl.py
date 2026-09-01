from __future__ import annotations

from backend.app.models.knowledge import (
    ACLEntry,
    Document,
    DocumentChunk,
    Permission,
    SubjectScope,
    SubjectType,
)


def can_access_document(subject: SubjectScope, document: Document) -> bool:
    if "admin" in subject.role_ids:
        return True
    return _matches_acl(subject, document.acl)


def can_access_chunk(
    subject: SubjectScope,
    chunk: DocumentChunk,
    document: Document,
) -> bool:
    if "admin" in subject.role_ids:
        return True
    # Chunk ACL defaults to the document ACL. This keeps filtering pre-retrieval.
    acl = chunk.acl or document.acl
    return _matches_acl(subject, acl)


def _matches_acl(subject: SubjectScope, acl: list[ACLEntry]) -> bool:
    for entry in acl:
        if entry.permission not in {Permission.READ, Permission.ADMIN}:
            continue
        if entry.subject_type == SubjectType.PUBLIC:
            return True
        if entry.subject_type == SubjectType.USER and entry.subject_id == subject.user_id:
            return True
        if (
            entry.subject_type == SubjectType.DEPARTMENT
            and entry.subject_id in subject.department_ids
        ):
            return True
        if entry.subject_type == SubjectType.ROLE and entry.subject_id in subject.role_ids:
            return True
    return False
