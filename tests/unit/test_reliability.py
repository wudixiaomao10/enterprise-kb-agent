from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path

from backend.app.backup import backup_local_objects, restore_local_objects
from backend.app.jobs.dlq import InMemoryDeadLetterQueue
from backend.app.jobs.models import IndexJobStatus
from backend.app.jobs.repository import InMemoryIndexJobRepository
from backend.app.jobs.service import IndexJobService


class FailingIngestion:
    def reindex_document(self, _document_id: str) -> dict[str, object]:
        raise RuntimeError("embedding backend unavailable")


class RecordingDispatcher:
    name = "test"

    def __init__(self) -> None:
        self.job_ids: list[str] = []

    def dispatch(self, job_id: str) -> None:
        self.job_ids.append(job_id)


class DeadLetterQueueTests(unittest.TestCase):
    def test_pending_entries_are_deduplicated_and_can_be_discarded(self) -> None:
        queue = InMemoryDeadLetterQueue()
        first = queue.enqueue(
            job_type="indexing",
            job_id="job-1",
            payload={"document_id": "doc-1"},
            error_type="RuntimeError",
            error_message="first failure",
            attempts=2,
        )
        updated = queue.enqueue(
            job_type="indexing",
            job_id="job-1",
            payload={"document_id": "doc-1", "version_id": "ver-1"},
            error_type="TimeoutError",
            error_message="latest failure",
            attempts=4,
        )

        self.assertEqual(first.dlq_id, updated.dlq_id)
        self.assertEqual(len(queue.list()), 1)
        self.assertEqual(queue.get(first.dlq_id).attempts, 4)  # type: ignore[union-attr]
        discarded = queue.discard(first.dlq_id)
        self.assertIsNotNone(discarded)
        self.assertEqual(queue.list(), [])
        self.assertEqual(len(queue.list(status="discarded")), 1)

    def test_terminal_index_failure_is_enqueued_and_replayable(self) -> None:
        repository = InMemoryIndexJobRepository()
        queue = InMemoryDeadLetterQueue()
        dispatcher = RecordingDispatcher()
        service = IndexJobService(
            repository,
            FailingIngestion(),  # type: ignore[arg-type]
            dispatcher,
            dlq=queue,
            max_attempts=2,
        )
        job = repository.create_job(
            document_id="doc-1",
            version_id="ver-1",
            requested_by="user-1",
        )

        with self.assertRaises(RuntimeError):
            service.execute(job.job_id)
        with self.assertRaises(RuntimeError):
            service.execute(job.job_id)

        failed = repository.get_job(job.job_id)
        self.assertIsNotNone(failed)
        self.assertEqual(failed.status, IndexJobStatus.FAILED)  # type: ignore[union-attr]
        entries = queue.list()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].attempts, 2)
        self.assertEqual(entries[0].payload["document_id"], "doc-1")

        replayed = service.replay_dead_letter(entries[0].dlq_id)
        self.assertIsNotNone(replayed)
        self.assertEqual(replayed.status, IndexJobStatus.QUEUED)  # type: ignore[union-attr]
        self.assertEqual(queue.get(entries[0].dlq_id).status, "replayed")  # type: ignore[union-attr]


class BackupRestoreTests(unittest.TestCase):
    def test_local_backup_restore_verifies_sha256_and_rejects_traversal(self) -> None:
        root = Path(".codex-tmp/unit-tests") / f"backup-{uuid.uuid4().hex}"
        source = root / "source"
        backup = root / "backup"
        restored = root / "restored"
        try:
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "policy.txt").write_text(
                "restricted policy", encoding="utf-8"
            )
            records = backup_local_objects(source, backup)
            self.assertEqual(len(records), 1)
            self.assertEqual(
                restore_local_objects(backup, restored, records),
                1,
            )
            self.assertEqual(
                (restored / "nested" / "policy.txt").read_text(encoding="utf-8"),
                "restricted policy",
            )
            with self.assertRaises(ValueError):
                restore_local_objects(
                    backup,
                    restored,
                    [{"relative_path": "../escape.txt", "sha256": "invalid"}],
                )
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
