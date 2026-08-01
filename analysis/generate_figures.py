"""Figure generation stubs for the article (matplotlib optional).

Run after a campaign exists under ``artifacts/raw/<id>/``. Prefer tables first;
figures are regenerated into ``artifacts/figures/<id>/``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--campaign-id", required=True)
    args = p.parse_args()
    out = REPO / "artifacts" / "figures" / args.campaign_id
    out.mkdir(parents=True, exist_ok=True)
    note = out / "README.md"
    note.write_text(
        f"# Figures for `{args.campaign_id}`\n\n"
        "Placeholder. Planned article figures:\n\n"
        "1. Lifecycle stacked bars (REG/SIQ/GRA/HBT)\n"
        "2. E2E latency across band profiles\n"
        "3. Cold-start stacked segments\n"
        "4. Persistence wall by backend × N\n"
        "5. Concurrency (deferred from frozen campaign)\n"
        "6. Resource/efficiency (deferred)\n\n"
        "Generate tables first: `analysis/generate_tables.py`.\n",
        encoding="utf-8",
    )
    print(f"Wrote {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
