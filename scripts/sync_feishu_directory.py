from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Synchronize the Feishu contact directory into PostgreSQL"
    )
    parser.add_argument(
        "--user-id",
        help="Only refresh one Feishu open_id; omit for a full directory sync",
    )
    args = parser.parse_args()

    load_dotenv(override=False)
    load_dotenv(ROOT / ".env.knowledge", override=False)

    from backend.app.bootstrap import create_worker_feishu_sync_service

    service = create_worker_feishu_sync_service()
    if service.directory.name != "postgres-directory":
        raise RuntimeError(
            "Feishu command-line sync requires KNOWLEDGE_STORE=postgres so the "
            "directory survives after this process exits"
        )
    result = service.sync(args.user_id)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
