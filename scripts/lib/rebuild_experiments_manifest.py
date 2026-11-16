#!/usr/bin/env python3
"""Write evidence/experiments/manifest.json and checksums.sha256."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "evidence" / "experiments"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    if not OUT.is_dir():
        print("missing evidence/experiments", file=sys.stderr)
        return 1

    selection = json.loads((OUT / "meta" / "selection_log.json").read_text(encoding="utf-8"))
    sources = selection["sources"]

    def files_under(*parts: str) -> list[str]:
        root = OUT.joinpath(*parts)
        if not root.exists():
            return []
        out = []
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.name not in ("checksums.sha256",):
                out.append(str(p.relative_to(OUT)))
        return out

    experiments = {
        "winnforum": {
            "category": "functional",
            "unit": "WInnForum test case",
            "n_expected": 65,
            "files": files_under("functional", "winnforum"),
            "provenance": {
                "source_id": "complementary",
                "agent_sha": sources["complementary"]["agent_sha"],
                "image_id": sources["complementary"]["image_id"],
                "executed_at_utc_meta": "see meta/campaigns/complementary/",
                "note": "Re-execution under frozen article image; not certification",
            },
        },
        "winnforum_failed_admin403": {
            "category": "functional",
            "unit": "failed harness attempt (provenance)",
            "files": files_under("functional", "winnforum_failed_admin403"),
            "provenance": {
                "source_id": "complementary",
                "note": "HTTP 403 on /admin/reset before admin fingerprint fix; retained",
            },
        },
        "isolation": {
            "category": "functional",
            "files": files_under("functional", "isolation"),
            "provenance": {"source_id": "full_integral", **{k: sources["full_integral"][k] for k in ("agent_sha", "image_id")}},
        },
        "cert_matrix": {
            "category": "functional",
            "files": files_under("functional", "cert_matrix"),
            "provenance": {"source_id": "full_integral", **{k: sources["full_integral"][k] for k in ("agent_sha", "image_id")}},
        },
        "boundary": {
            "category": "functional",
            "files": files_under("functional", "boundary"),
            "provenance": {"source_id": "full_integral_or_historical"},
        },
        "robustness": {
            "category": "functional",
            "files": files_under("functional", "robustness"),
            "provenance": {"source_id": "full_integral_or_historical"},
        },
        "lifecycle": {
            "category": "lifecycle",
            "independent_unit": "Docker deployment (profile × rep)",
            "files": files_under("lifecycle"),
            "provenance": {"source_id": "full_integral", **{k: sources["full_integral"][k] for k in ("agent_sha", "image_id")}},
        },
        "coldstart": {
            "category": "coldstart",
            "independent_unit": "container cold start",
            "files": files_under("coldstart"),
            "provenance": {"source_id": "full_integral", **{k: sources["full_integral"][k] for k in ("agent_sha", "image_id")}},
        },
        "tls_mtls": {
            "category": "security",
            "independent_unit": "TLS/mTLS request block",
            "files": files_under("security", "tls_mtls"),
            "provenance": {"source_id": "full_integral", **{k: sources["full_integral"][k] for k in ("agent_sha", "image_id")}},
        },
        "persistence": {
            "category": "persistence",
            "files": files_under("persistence"),
            "provenance": {"source_id": "full_integral", **{k: sources["full_integral"][k] for k in ("agent_sha", "image_id")}},
        },
        "factorial": {
            "category": "factorial",
            "files": files_under("factorial"),
            "provenance": {"source_id": "full_integral", **{k: sources["full_integral"][k] for k in ("agent_sha", "image_id")}},
        },
        "process_concurrency": {
            "category": "concurrency",
            "files": files_under("concurrency", "processes"),
            "provenance": {"source_id": "full_integral", **{k: sources["full_integral"][k] for k in ("agent_sha", "image_id")}},
        },
        "container_concurrency_calibration": {
            "category": "concurrency",
            "role": "calibration",
            "files": files_under("concurrency", "containers", "calibration"),
            "provenance": {"source_id": "full_integral", **{k: sources["full_integral"][k] for k in ("agent_sha", "image_id")}},
        },
        "container_per_agent": {
            "category": "concurrency",
            "files": files_under("concurrency", "per_agent"),
            "provenance": {"source_id": "full_integral", **{k: sources["full_integral"][k] for k in ("agent_sha", "image_id")}},
        },
        "provisioning_calibration": {
            "category": "provisioning",
            "role": "calibration",
            "same_files_as": "concurrency/containers/calibration",
            "files": files_under("provisioning", "calibration"),
            "provenance": {"source_id": "full_integral", **{k: sources["full_integral"][k] for k in ("agent_sha", "image_id")}},
        },
        "k3_independent_validation": {
            "category": "provisioning",
            "role": "independent_validation",
            "selected_k": 3,
            "seed_base": 94242,
            "n_repeats": 5,
            "files": files_under("provisioning", "validation", "k3_independent"),
            "provenance": {"source_id": "complementary", **{k: sources["complementary"][k] for k in ("agent_sha", "image_id")}},
            "limits_package": {
                "name": "illustrative_P4",
                "startup_sec": 60,
                "provisioning_sec": 1.5,
                "grant_batch_p95_ms": 1000,
                "memory_mb": 2048,
                "limits_are_illustrative": True,
                "do_not_retune_after_observation": True,
            },
        },
        "historical_k1_k15_validation": {
            "category": "provisioning",
            "role": "historical_independent_validation",
            "files": files_under("provisioning", "validation", "historical_k1_k15"),
            "provenance": {
                "source_id": "historical",
                "note": "Preserved; not merged with K=3 validation rows",
            },
        },
        "k5_illustrative_sensitivity": {
            "category": "provisioning",
            "role": "sensitivity_not_selected_k",
            "files": files_under("provisioning", "sensitivity", "k5_illustrative"),
            "provenance": {"source_id": "full_integral", **{k: sources["full_integral"][k] for k in ("agent_sha", "image_id")}},
        },
        "sustained_single_agent": {
            "category": "sustained",
            "independent_unit": "seeded sustained window (not individual HTTP requests)",
            "files": files_under("sustained", "single_agent"),
            "provenance": {"source_id": "full_integral", **{k: sources["full_integral"][k] for k in ("agent_sha", "image_id")}},
        },
        "colocated_mixed": {
            "category": "sustained",
            "independent_unit": "paired scenario (A=K1 vs B=K5), five pairs",
            "n_pairs": 5,
            "arrival_interval_sec": 10,
            "relinquish_interval_sec": 30,
            "files": files_under("sustained", "colocated"),
            "provenance": {"source_id": "complementary", **{k: sources["complementary"][k] for k in ("agent_sha", "image_id")}},
        },
        "sqlite_recovery": {
            "category": "recovery",
            "independent_unit": "recovery cycle",
            "files": files_under("recovery", "sqlite"),
            "provenance": {"source_id": "full_integral", **{k: sources["full_integral"][k] for k in ("agent_sha", "image_id")}},
        },
    }

    # Count winnforum passes
    wf_csv = OUT / "functional" / "winnforum" / "lifecycle_validation.csv"
    if wf_csv.is_file():
        import csv

        rows = list(csv.DictReader(wf_csv.open(encoding="utf-8")))
        experiments["winnforum"]["n_observed"] = len(rows)
        experiments["winnforum"]["results"] = {
            r.get("result", ""): sum(1 for x in rows if x.get("result") == r.get("result"))
            for r in rows
        }
        # simpler
        from collections import Counter

        experiments["winnforum"]["results"] = dict(Counter(r.get("result") for r in rows))

    manifest = {
        "schema_version": "2.0",
        "collection_id": "experiments",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "purpose": "Single published experimental collection for the SAS Agent",
        "layout": "evidence/experiments/<category>/...",
        "sources": sources,
        "relations": {
            "calibration_to_validation": {
                "calibration": "provisioning/calibration (and concurrency/containers/calibration)",
                "validation_selected_k": "provisioning/validation/k3_independent",
                "do_not_pool_as_one_campaign": True,
            },
            "illustrative_limits": {
                "startup_sec": 60,
                "provisioning_sec": 1.5,
                "grant_batch_p95_ms": 1000,
                "memory_mb": 2048,
                "note": "Illustrative limits originally studied with historical calibration; new calibration may select K≠5. Limits are not retuned after observing validation.",
            },
        },
        "experiments": experiments,
        "analysis_stage": "analysis_stage/",
        "derived_outputs": "analysis/",
        "exclusions": selection.get("exclusions", []),
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # checksums of all files except checksums itself
    lines = []
    for p in sorted(OUT.rglob("*")):
        if not p.is_file():
            continue
        if p.name == "checksums.sha256":
            continue
        if p.is_symlink():
            continue
        rel = p.relative_to(OUT)
        lines.append(f"{sha256(p)}  {rel.as_posix()}")
    (OUT / "checksums.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote manifest + {len(lines)} checksums")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
