#!/usr/bin/env python3
"""Build the single published experimental collection under evidence/experiments/.

Copies selected raw artifacts from existing campaign trees. Does not re-run
benchmarks or mutate source CSVs. Writes provenance, selection log, manifest,
and checksums. Safe to re-run (rebuilds destination).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "evidence" / "experiments"
FULL = REPO / "evidence" / "tnsm-2026-12135-full-20261112T200619Z"
COMP = REPO / "evidence" / "tnsm-2026-12135-complementary-20261113T141440Z"
HIST = REPO / "evidence" / "2026-12135"

SKIP_SUFFIXES = {".key", ".pem"}
SKIP_NAME_PARTS = ("harness_certs",)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _should_skip(path: Path) -> bool:
    if path.suffix.lower() in SKIP_SUFFIXES:
        return True
    return any(part in path.parts for part in SKIP_NAME_PARTS)


def copy_file(src: Path, dst: Path, copied: list[dict], *, role: str, source_id: str) -> None:
    if not src.is_file():
        raise FileNotFoundError(src)
    if _should_skip(src):
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    copied.append(
        {
            "dest": str(dst.relative_to(OUT)),
            "source": str(src.relative_to(REPO)),
            "sha256": _sha256(dst),
            "bytes": dst.stat().st_size,
            "role": role,
            "source_id": source_id,
        }
    )


def copy_tree(src: Path, dst: Path, copied: list[dict], *, role: str, source_id: str) -> None:
    if not src.is_dir():
        raise FileNotFoundError(src)
    for path in sorted(src.rglob("*")):
        if not path.is_file() or _should_skip(path):
            continue
        rel = path.relative_to(src)
        copy_file(path, dst / rel, copied, role=role, source_id=source_id)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def main() -> int:
    if not FULL.is_dir() or not COMP.is_dir():
        print("missing FULL or COMP campaign trees", file=sys.stderr)
        return 1

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    copied: list[dict] = []
    exclusions: list[dict] = []

    full_sha = read_text(FULL / "meta" / "agent_sha.txt")
    full_img = read_text(FULL / "meta" / "image_id.txt")
    comp_sha = read_text(COMP / "meta" / "agent_sha.txt")
    comp_img = read_text(COMP / "meta" / "image_id.txt")

    # ---- functional ----
    copy_tree(
        COMP / "02_functional" / "winnforum_reexec_adminfix",
        OUT / "functional" / "winnforum",
        copied,
        role="winnforum_reexecution",
        source_id="complementary",
    )
    # keep failed attempt as provenance-only (scientific relevance of harness fix)
    copy_tree(
        COMP / "02_functional" / "winnforum_reexec",
        OUT / "functional" / "winnforum_failed_admin403",
        copied,
        role="winnforum_failed_attempt_provenance",
        source_id="complementary",
    )
    for name in ("isolation", "cert_matrix", "boundary", "robustness"):
        src = FULL / "02_functional" / name
        if not src.is_dir():
            # boundary/robustness may only exist in historical corpus
            src = HIST / "functional" / name
            sid = "historical"
        else:
            sid = "full_integral"
        if src.is_dir():
            copy_tree(src, OUT / "functional" / name, copied, role=name, source_id=sid)
        else:
            exclusions.append({"item": name, "reason": "source missing"})

    # ---- lifecycle / coldstart / security(tls) / persistence / factorial ----
    copy_tree(FULL / "04_lifecycle", OUT / "lifecycle", copied, role="lifecycle", source_id="full_integral")
    copy_tree(FULL / "03_coldstart", OUT / "coldstart", copied, role="coldstart", source_id="full_integral")
    copy_tree(FULL / "05_tls", OUT / "security" / "tls_mtls", copied, role="tls_mtls", source_id="full_integral")
    if (FULL / "01_tests").is_dir():
        copy_tree(FULL / "01_tests", OUT / "security" / "unit_logs", copied, role="security_unit_logs", source_id="full_integral")
    copy_tree(FULL / "06_persistence", OUT / "persistence", copied, role="persistence", source_id="full_integral")
    copy_tree(FULL / "07_factorial", OUT / "factorial", copied, role="factorial", source_id="full_integral")

    # ---- concurrency ----
    copy_tree(
        FULL / "08_concurrency" / "processes",
        OUT / "concurrency" / "processes",
        copied,
        role="process_concurrency",
        source_id="full_integral",
    )
    copy_tree(
        FULL / "08_concurrency" / "containers",
        OUT / "concurrency" / "containers" / "calibration",
        copied,
        role="container_concurrency_calibration",
        source_id="full_integral",
    )
    copy_tree(
        FULL / "08_concurrency" / "per_agent",
        OUT / "concurrency" / "per_agent",
        copied,
        role="container_per_agent",
        source_id="full_integral",
    )

    # ---- provisioning ----
    # calibration = same container concurrency calib (symlink-equivalent copy pointer in manifest)
    copy_tree(
        FULL / "08_concurrency" / "containers",
        OUT / "provisioning" / "calibration",
        copied,
        role="provisioning_calibration",
        source_id="full_integral",
    )
    copy_tree(
        COMP / "12_provisioning" / "k3_validation",
        OUT / "provisioning" / "validation" / "k3_independent",
        copied,
        role="k3_independent_validation",
        source_id="complementary",
    )
    # Historical independent validation (K=1, K=15) — not replaced by K=3 package
    if (HIST / "concurrency" / "containers" / "validation").is_dir():
        copy_tree(
            HIST / "concurrency" / "containers" / "validation",
            OUT / "provisioning" / "validation" / "historical_k1_k15",
            copied,
            role="historical_independent_validation",
            source_id="historical",
        )
    # FULL k5 sensitivity (additional, not selected-K validation)
    if (FULL / "12_provisioning" / "k5_validation").is_dir():
        copy_tree(
            FULL / "12_provisioning" / "k5_validation",
            OUT / "provisioning" / "sensitivity" / "k5_illustrative",
            copied,
            role="k5_sensitivity_not_selected_k",
            source_id="full_integral",
        )

    # ---- sustained ----
    copy_tree(
        FULL / "09_sustained",
        OUT / "sustained" / "single_agent",
        copied,
        role="sustained_single_agent",
        source_id="full_integral",
    )
    copy_tree(
        COMP / "10_colocated",
        OUT / "sustained" / "colocated",
        copied,
        role="colocated_mixed_operations",
        source_id="complementary",
    )
    exclusions.append(
        {
            "item": "full_integral/10_colocated",
            "reason": "lacks mid-window Registration/Grant/Relinquish mix (arrivals_sum=0); replaced by complementary colocated",
        }
    )
    exclusions.append(
        {
            "item": "pilots/smoke colocated",
            "reason": "not definitive evaluation units",
        }
    )

    # ---- recovery ----
    copy_tree(
        FULL / "11_recovery",
        OUT / "recovery" / "sqlite",
        copied,
        role="http_mtls_sqlite_recovery",
        source_id="full_integral",
    )

    # ---- meta / host telemetry ----
    (OUT / "meta").mkdir(parents=True, exist_ok=True)
    for label, root, sid in (("full_integral", FULL, "full_integral"), ("complementary", COMP, "complementary")):
        dest = OUT / "meta" / "campaigns" / label
        dest.mkdir(parents=True, exist_ok=True)
        for name in (
            "agent_sha.txt",
            "image_id.txt",
            "protocol_frozen.json",
            "preflight_host.txt",
            "preflight_article.txt",  # legacy filename from earlier campaigns
            "preflight_snapshot.json",
            "attempts.tsv",
            "inventory.json",
        ):
            src = root / "meta" / name
            if src.is_file():
                copy_file(src, dest / name, copied, role=f"meta_{label}", source_id=sid)
        # stage markers
        stages = root / "meta" / "stages"
        if stages.is_dir():
            copy_tree(stages, dest / "stages", copied, role=f"stages_{label}", source_id=sid)

    # host telemetry gates (both)
    for label, root, sid in (("full_integral", FULL, "full_integral"), ("complementary", COMP, "complementary")):
        ht = root / "host_telemetry"
        if ht.is_dir():
            copy_tree(ht, OUT / "meta" / "host_telemetry" / label, copied, role="host_telemetry", source_id=sid)

    # selection / exclusion log
    selection = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "destination": "evidence/experiments",
        "sources": {
            "full_integral": {
                "path": str(FULL.relative_to(REPO)),
                "agent_sha": full_sha,
                "image_id": full_img,
            },
            "complementary": {
                "path": str(COMP.relative_to(REPO)),
                "agent_sha": comp_sha,
                "image_id": comp_img,
            },
            "historical": {
                "path": str(HIST.relative_to(REPO)) if HIST.is_dir() else None,
                "note": "used only for boundary/robustness gaps and historical K1/K15 validation",
            },
        },
        "exclusions": exclusions,
        "files": copied,
        "note": (
            "Published collection is organized by technical experiment category. "
            "Do not merge rows across source campaigns as if they were one session."
        ),
    }
    (OUT / "meta" / "selection_log.json").write_text(
        json.dumps(selection, indent=2) + "\n", encoding="utf-8"
    )

    # ---- analysis placeholders ----
    (OUT / "analysis").mkdir(parents=True, exist_ok=True)

    # ---- staging farm for tools expecting artifacts/raw layout ----
    stage = OUT / "analysis_stage"
    stage.mkdir(parents=True, exist_ok=True)
    links = {
        "lifecycle_decomposition": OUT / "lifecycle",
        "instrumented_cold_start": OUT / "coldstart",
        "mtls_correctness_overhead": OUT / "security" / "tls_mtls",
        "cert_matrix": OUT / "functional" / "cert_matrix",
        "persistence_decomposition": OUT / "persistence",
        "factorial_provisioning": OUT / "factorial",
        "concurrent_slicing_v2": OUT / "concurrency" / "processes",
        "container_concurrency": OUT / "concurrency" / "containers" / "calibration",
    }
    for name, target in links.items():
        link = stage / name
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(Path("..") / target.relative_to(OUT))

    # campaign_manifest for validators
    camp_man = {
        "campaign_id": "experiments",
        "campaign_type": "published_experimental_collection",
        "experiments": {k: str(v.relative_to(OUT)) for k, v in links.items()},
        "phases": sorted(
            {
                "functional",
                "lifecycle",
                "coldstart",
                "security",
                "persistence",
                "factorial",
                "concurrency",
                "provisioning",
                "sustained",
                "recovery",
                "analysis",
            }
        ),
        "sas_agent_sha_primary": full_sha,
        "image_id_primary": full_img,
        "note": "Multiple source commits; see manifest.json experiments[].provenance",
    }
    (stage / "campaign_manifest.json").write_text(
        json.dumps(camp_man, indent=2) + "\n", encoding="utf-8"
    )

    print(f"copied {len(copied)} files → {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
