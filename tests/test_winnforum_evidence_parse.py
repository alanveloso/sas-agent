"""Traceability of the article's 65 WInnForum cases (parse-only; no harness).

The Aug-2026 evidence logs under ``benchmarks/data/`` are local and gitignored.
When present, this test locks the claim that those logs contain 65 passing
CBSD lifecycle cases (REG+SIQ+GRA+HBT+RLQ+DRG). It does not re-run the harness
and does not assert equivalence with the unit-test suite.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))

import parse_winnforum_logs as parser  # noqa: E402

EVIDENCE = ROOT / "benchmarks" / "data"
EXPECTED_SUITES = {
    "WINNF_FT_S_REG_testcase": 13,
    "WINNF_FT_S_SIQ_testcase": 12,
    "WINNF_FT_S_GRA_testcase": 16,
    "WINNF_FT_S_HBT_testcase": 12,
    "WINNF_FT_S_RLQ_testcase": 6,
    "WINNF_FT_S_DRG_testcase": 6,
}


@unittest.skipUnless(
    (EVIDENCE / "winnforum_WINNF_FT_S_REG_testcase.log").is_file(),
    "local WInnForum evidence logs not present (gitignored)",
)
class WinnForumEvidenceParseTests(unittest.TestCase):
    def test_article_evidence_has_65_passing_cases(self):
        rows = []
        for log in sorted(EVIDENCE.glob("winnforum_*.log")):
            rows.extend(parser.parse_log(log))
        self.assertEqual(len(rows), 65)
        self.assertEqual(Counter(r["result"] for r in rows), {"pass": 65})
        by_suite = Counter(r["suite"] for r in rows)
        self.assertEqual(dict(by_suite), EXPECTED_SUITES)

    def test_parser_round_trip_to_csv(self):
        out = Path(tempfile.mkdtemp(prefix="winnforum_parse_")) / "lifecycle.csv"
        rc = parser.main(["--logs-dir", str(EVIDENCE), "--out", str(out)])
        self.assertEqual(rc, 0)
        text = out.read_text(encoding="utf-8")
        self.assertEqual(text.count("\n") - 1, 65)  # header + 65 rows


if __name__ == "__main__":
    unittest.main()
