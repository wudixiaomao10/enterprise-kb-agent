from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    load_dotenv(override=False)
    load_dotenv(ROOT / ".env.knowledge", override=False)
    from backend.app.bootstrap import create_worker_graph_sync_service

    service = create_worker_graph_sync_service()
    result = service.handle_lifecycle_notifications()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
