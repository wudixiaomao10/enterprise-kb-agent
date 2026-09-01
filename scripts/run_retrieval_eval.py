from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.bootstrap import create_demo_services
from backend.app.evaluation.retrieval_eval import (
    load_eval_cases,
    run_retrieval_evaluation,
)


def main() -> None:
    _, _, qa = create_demo_services()
    cases = load_eval_cases(Path("docs/evaluation/retrieval_eval.json"))
    report = run_retrieval_evaluation(qa, cases)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
