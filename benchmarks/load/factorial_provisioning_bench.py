"""Fatorial controlado: efeito da faixa × população no provisionamento (process-level).

Células A–D × ``--repeats`` (ordem randomizada). Cada execução roda em processo
filho com SQLite próprio (evita estado compartilhado do engine).

Saídas: factorial_provisioning_{runs,summary,manifest}.*
"""

from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

LOAD_DIR = Path(__file__).resolve().parent
BENCH_DIR = LOAD_DIR.parent
ROOT = BENCH_DIR.parent
for p in (str(ROOT), str(LOAD_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from common import (  # noqa: E402
    DEFAULT_SEED,
    capture_manifest,
    new_run_id,
    percentile,
    utc_now_iso,
    write_manifest,
)

DATA = BENCH_DIR / "data"
CHANNEL_WIDTH_HZ = 10_000_000

CELLS: list[dict[str, Any]] = [
    {"config_id": "A", "band_low_hz": 3_550_000_000, "band_high_hz": 3_570_000_000, "devices": 133, "synthetic": False},
    {"config_id": "B", "band_low_hz": 3_550_000_000, "band_high_hz": 3_570_000_000, "devices": 1000, "synthetic": True},
    {"config_id": "C", "band_low_hz": 3_550_000_000, "band_high_hz": 3_700_000_000, "devices": 133, "synthetic": False},
    {"config_id": "D", "band_low_hz": 3_550_000_000, "band_high_hz": 3_700_000_000, "devices": 1000, "synthetic": False},
]

RUN_FIELDS = [
    "run_id", "timestamp_utc", "repeat", "config_id", "order_in_block",
    "band_low_hz", "band_high_hz", "band_width_mhz", "devices",
    "synthetic_isolation_cell", "profile_load_ms", "device_population_wall_sec",
    "probe_grant_mean_ms", "probe_grant_p95_ms", "probe_samples",
    "exit_code", "error",
]
SUMMARY_FIELDS = [
    "config_id", "band_width_mhz", "devices", "n_repeats", "metric",
    "median", "iqr", "min", "max", "mean",
]


def _channels(low: int, high: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    f = low
    while f + CHANNEL_WIDTH_HZ <= high:
        out.append((f, f + CHANNEL_WIDTH_HZ))
        f += CHANNEL_WIDTH_HZ
    return out or [(low, high)]


def _reg(fcc: str, user: str, serial: str) -> dict[str, Any]:
    return {
        "userId": user,
        "fccId": fcc,
        "cbsdSerialNumber": serial,
        "cbsdCategory": "A",
        "airInterface": {"radioTechnology": "E_UTRA"},
        "installationParam": {
            "latitude": 37.0,
            "longitude": -122.0,
            "height": 6.0,
            "heightType": "AGL",
            "indoorDeployment": True,
            "antennaGain": 0,
        },
    }


def _rc(items: list[dict[str, Any]]) -> int:
    return items[0].get("response", {}).get("responseCode", -1)


def _cell_worker(payload: dict[str, Any], out_q: Any) -> None:
    """Child process: isolated profile + DB for one factorial cell."""
    import os

    cell = payload["cell"]
    seed = payload["seed"]
    probe_n = payload["probe_n"]
    root = payload["root"]
    run_id = payload["run_id"]

    result: dict[str, Any] = {
        "run_id": run_id,
        "timestamp_utc": utc_now_iso(),
        "config_id": cell["config_id"],
        "band_low_hz": cell["band_low_hz"],
        "band_high_hz": cell["band_high_hz"],
        "band_width_mhz": (cell["band_high_hz"] - cell["band_low_hz"]) / 1e6,
        "devices": cell["devices"],
        "synthetic_isolation_cell": int(cell["synthetic"]),
        "profile_load_ms": "",
        "device_population_wall_sec": "",
        "probe_grant_mean_ms": "",
        "probe_grant_p95_ms": "",
        "probe_samples": 0,
        "exit_code": 0,
        "error": "",
    }
    try:
        with tempfile.TemporaryDirectory(prefix=f"fact_{cell['config_id']}_") as tmp:
            os.environ["SAS_DATABASE_PATH"] = str(Path(tmp) / "sas.db")
            os.environ.pop("BAND_PROFILE_FILE", None)
            os.environ.pop("BAND_LOW", None)
            os.environ.pop("BAND_HIGH", None)
            if root not in sys.path:
                sys.path.insert(0, root)

            import database
            from domain.band_profile import cbrs_winnforum_profile
            from models.models import FccIdRecord, UserIdRecord
            from services.grant_service import process_grant
            from services.profile_manager import (
                reset_active_band_profile,
                set_active_band_profile,
            )
            from services.registration_service import process_registration
            from services.relinquishment_service import process_relinquishment

            t0 = time.perf_counter()
            reset_active_band_profile()
            profile = cbrs_winnforum_profile().with_band_limits(
                cell["band_low_hz"], cell["band_high_hz"]
            )
            set_active_band_profile(profile)
            database.init_db()
            result["profile_load_ms"] = round((time.perf_counter() - t0) * 1000.0, 3)

            fcc = f"fact_{cell['config_id']}_{seed}"
            user = f"fact_op_{cell['config_id']}"
            db = database.SessionLocal()
            try:
                db.add(FccIdRecord(fcc_id=fcc, fcc_max_eirp=47.0))
                db.add(UserIdRecord(user_id=user))
                db.commit()
            finally:
                db.close()

            channels = _channels(cell["band_low_hz"], cell["band_high_hz"])
            t_pop = time.perf_counter()
            for i in range(cell["devices"]):
                serial = f"sn_{seed}_{i:05d}"
                low, high = channels[i % len(channels)]
                db = database.SessionLocal()
                try:
                    reg = process_registration(db, [_reg(fcc, user, serial)], profile=profile)
                    if _rc(reg) != 0:
                        raise RuntimeError(f"reg rc={_rc(reg)}")
                    cbsd_id = reg[0]["cbsdId"]
                    gr = process_grant(
                        db,
                        [{
                            "cbsdId": cbsd_id,
                            "operationParam": {
                                "maxEirp": 20.0,
                                "operationFrequencyRange": {
                                    "lowFrequency": low,
                                    "highFrequency": high,
                                },
                            },
                        }],
                        profile=profile,
                    )
                    if _rc(gr) != 0:
                        raise RuntimeError(f"grant rc={_rc(gr)}")
                finally:
                    db.close()
            result["device_population_wall_sec"] = round(time.perf_counter() - t_pop, 3)

            db = database.SessionLocal()
            try:
                reg = process_registration(
                    db, [_reg(fcc, user, f"probe_{seed}")], profile=profile
                )
                if _rc(reg) != 0:
                    raise RuntimeError(f"probe reg rc={_rc(reg)}")
                probe_id = reg[0]["cbsdId"]
            finally:
                db.close()

            probe_low, probe_high = channels[0]
            grant_lats: list[float] = []
            for _ in range(probe_n):
                db = database.SessionLocal()
                try:
                    t_g = time.perf_counter()
                    gr = process_grant(
                        db,
                        [{
                            "cbsdId": probe_id,
                            "operationParam": {
                                "maxEirp": 20.0,
                                "operationFrequencyRange": {
                                    "lowFrequency": probe_low,
                                    "highFrequency": probe_high,
                                },
                            },
                        }],
                        profile=profile,
                    )
                    lat = (time.perf_counter() - t_g) * 1000.0
                    if _rc(gr) != 0:
                        raise RuntimeError(f"probe grant rc={_rc(gr)}")
                    grant_lats.append(lat)
                    gid = gr[0].get("grantId")
                    if gid:
                        process_relinquishment(
                            db, [{"cbsdId": probe_id, "grantId": gid}]
                        )
                finally:
                    db.close()
            result["probe_samples"] = len(grant_lats)
            result["probe_grant_mean_ms"] = round(statistics.fmean(grant_lats), 3)
            result["probe_grant_p95_ms"] = round(percentile(grant_lats, 95) or 0.0, 3)
    except Exception as exc:  # noqa: BLE001
        result["exit_code"] = 1
        result["error"] = str(exc)[:300]
    out_q.put(result)


def _run_cell(cell: dict[str, Any], seed: int, probe_n: int, timeout: float) -> dict[str, Any]:
    ctx = mp.get_context("spawn")
    q: Any = ctx.Queue()
    payload = {
        "cell": cell,
        "seed": seed,
        "probe_n": probe_n,
        "root": str(ROOT),
        "run_id": new_run_id(),
    }
    proc = ctx.Process(target=_cell_worker, args=(payload, q), daemon=False)
    proc.start()
    try:
        result = q.get(timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        result = {
            "run_id": payload["run_id"],
            "timestamp_utc": utc_now_iso(),
            "config_id": cell["config_id"],
            "band_low_hz": cell["band_low_hz"],
            "band_high_hz": cell["band_high_hz"],
            "band_width_mhz": (cell["band_high_hz"] - cell["band_low_hz"]) / 1e6,
            "devices": cell["devices"],
            "synthetic_isolation_cell": int(cell["synthetic"]),
            "profile_load_ms": "",
            "device_population_wall_sec": "",
            "probe_grant_mean_ms": "",
            "probe_grant_p95_ms": "",
            "probe_samples": 0,
            "exit_code": 2,
            "error": f"queue timeout/error: {exc}",
        }
    proc.join(timeout=30)
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=10)
    proc.close()
    q.close()
    return result


def _iqr(vals: list[float]) -> float:
    return (percentile(vals, 75) or 0.0) - (percentile(vals, 25) or 0.0)


def _aggregate(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    metrics = ("profile_load_ms", "device_population_wall_sec", "probe_grant_mean_ms")
    by_cfg: dict[str, list[dict[str, Any]]] = {}
    for r in runs:
        by_cfg.setdefault(r["config_id"], []).append(r)
    out: list[dict[str, Any]] = []
    for cfg_id, rows in sorted(by_cfg.items()):
        for metric in metrics:
            vals = [float(r[metric]) for r in rows if r.get(metric) not in ("", None)]
            if not vals:
                continue
            out.append({
                "config_id": cfg_id,
                "band_width_mhz": rows[0]["band_width_mhz"],
                "devices": rows[0]["devices"],
                "n_repeats": len(vals),
                "metric": metric,
                "median": round(float(statistics.median(vals)), 4),
                "iqr": round(_iqr(vals), 4),
                "min": round(min(vals), 4),
                "max": round(max(vals), 4),
                "mean": round(statistics.fmean(vals), 4),
            })
    return out


def _shapiro_wilk(x: list[float]) -> dict[str, float] | None:
    try:
        from scipy import stats  # type: ignore
    except ImportError:
        return None
    if len(x) < 3:
        return None
    w, p = stats.shapiro(x)
    return {"W": float(w), "p": float(p)}


def _levene(groups: list[list[float]]) -> dict[str, float] | None:
    try:
        from scipy import stats  # type: ignore
    except ImportError:
        return None
    if any(len(g) < 2 for g in groups):
        return None
    w, p = stats.levene(*groups)
    return {"W": float(w), "p": float(p)}


def _two_way_anova(runs: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    """Balanced 2×2 CRD ANOVA (width × devices) with η².

    This treats every cell observation as an independent CRD sample and
    **ignores the randomized-block structure** of the harness (``repeat``).
    Prefer :func:`_blocked_log_contrasts` as the primary inference for article
    claims; keep this ANOVA only as a supplementary contrast that explains why
    earlier summaries reported a significant interaction.
    """
    cells: dict[tuple[float, int], list[float]] = {}
    for r in runs:
        if r.get("exit_code") != 0:
            continue
        val = r.get(metric)
        if val in ("", None):
            continue
        key = (float(r["band_width_mhz"]), int(r["devices"]))
        cells.setdefault(key, []).append(float(val))
    widths = sorted({w for w, _ in cells})
    pops = sorted({d for _, d in cells})
    if len(widths) != 2 or len(pops) != 2:
        return {"error": f"expected 2x2 cells, got widths={widths} devices={pops}"}
    n_cell = min(len(cells[(w, d)]) for w in widths for d in pops)
    if n_cell < 2:
        return {"error": "need >=2 repeats per cell"}
    grid = [[cells[(w, d)][:n_cell] for d in pops] for w in widths]
    a, b, n = 2, 2, n_cell
    grand = statistics.fmean(v for row in grid for cell in row for v in cell)
    row_means = [statistics.fmean(v for cell in row for v in cell) for row in grid]
    col_means = [
        statistics.fmean(grid[i][j][k] for i in range(a) for k in range(n))
        for j in range(b)
    ]
    ss_a = b * n * sum((m - grand) ** 2 for m in row_means)
    ss_b = a * n * sum((m - grand) ** 2 for m in col_means)
    ss_ab = 0.0
    for i in range(a):
        for j in range(b):
            cell_mean = statistics.fmean(grid[i][j])
            ss_ab += n * (cell_mean - row_means[i] - col_means[j] + grand) ** 2
    ss_e = 0.0
    residuals: list[float] = []
    groups: list[list[float]] = []
    for i in range(a):
        for j in range(b):
            cm = statistics.fmean(grid[i][j])
            groups.append(list(grid[i][j]))
            for v in grid[i][j]:
                ss_e += (v - cm) ** 2
                residuals.append(v - cm)
    ss_t = ss_a + ss_b + ss_ab + ss_e
    df_a, df_b, df_ab, df_e = a - 1, b - 1, (a - 1) * (b - 1), a * b * (n - 1)
    ms_e = ss_e / df_e if df_e else float("nan")
    f_a = (ss_a / df_a) / ms_e if ms_e else float("inf")
    f_b = (ss_b / df_b) / ms_e if ms_e else float("inf")
    f_ab = (ss_ab / df_ab) / ms_e if ms_e else float("inf")

    def _f_p(f: float, d1: int, d2: int) -> float | None:
        try:
            from scipy import stats  # type: ignore
            return float(stats.f.sf(f, d1, d2))
        except ImportError:
            return None

    return {
        "metric": metric,
        "design_note": (
            "completely_randomized_two_way_anova; ignores randomized blocks "
            "(repeat). Not the primary inference for this harness."
        ),
        "factors": {
            "A": "band_width_mhz",
            "B": "devices",
            "levels_A": widths,
            "levels_B": pops,
        },
        "n_per_cell": n_cell,
        "df": {"A": df_a, "B": df_b, "AB": df_ab, "error": df_e},
        "F": {
            "A_width": round(f_a, 4),
            "B_devices": round(f_b, 4),
            "AB_interaction": round(f_ab, 4),
        },
        "p": {
            "A_width": _f_p(f_a, df_a, df_e),
            "B_devices": _f_p(f_b, df_b, df_e),
            "AB_interaction": _f_p(f_ab, df_ab, df_e),
        },
        "eta_squared": {
            "A_width": round(ss_a / ss_t, 6) if ss_t else None,
            "B_devices": round(ss_b / ss_t, 6) if ss_t else None,
            "AB_interaction": round(ss_ab / ss_t, 6) if ss_t else None,
        },
        "shapiro_residuals": _shapiro_wilk(residuals),
        "levene_cells": _levene(groups),
    }


def _blocked_log_contrasts(
    runs: list[dict[str, Any]],
    metric: str = "device_population_wall_sec",
    *,
    bootstrap_n: int = 20_000,
    seed: int = 42,
) -> dict[str, Any]:
    """Primary 2×2 inference for randomized blocks (config A–D within ``repeat``).

    For each complete block, compute log-contrasts:

    - width: 0.5 * ((log C - log A) + (log D - log B))
    - population: 0.5 * ((log B - log A) + (log D - log C))
    - interaction: (log D - log C) - (log B - log A)

    Geometric-mean effect = exp(mean contrast). Bootstrap resamples blocks.
    """
    import math

    by_block: dict[int, dict[str, float]] = {}
    for r in runs:
        if int(r.get("exit_code", 1)) != 0:
            continue
        val = r.get(metric)
        if val in ("", None):
            continue
        rep = int(r["repeat"])
        by_block.setdefault(rep, {})[str(r["config_id"])] = float(val)

    complete = {
        rep: cells
        for rep, cells in sorted(by_block.items())
        if set(cells) >= {"A", "B", "C", "D"}
    }
    if not complete:
        return {"error": "no complete randomized blocks with configs A–D"}

    width_c: list[float] = []
    pop_c: list[float] = []
    inter_c: list[float] = []
    for cells in complete.values():
        la, lb, lc, ld = (math.log(cells[k]) for k in "ABCD")
        width_c.append(0.5 * ((lc - la) + (ld - lb)))
        pop_c.append(0.5 * ((lb - la) + (ld - lc)))
        inter_c.append((ld - lc) - (lb - la))

    def _wilcoxon_p(contrasts: list[float]) -> float | None:
        try:
            from scipy import stats  # type: ignore
        except ImportError:
            return None
        if all(abs(c) < 1e-15 for c in contrasts):
            return 1.0
        try:
            return float(stats.wilcoxon(contrasts, alternative="two-sided").pvalue)
        except ValueError:
            return None

    def _effect(contrasts: list[float], name: str) -> dict[str, Any]:
        mean_c = statistics.fmean(contrasts)
        rng = random.Random(seed)
        boots: list[float] = []
        n = len(contrasts)
        for _ in range(bootstrap_n):
            sample = [contrasts[rng.randrange(n)] for _ in range(n)]
            boots.append(math.exp(statistics.fmean(sample)))
        boots.sort()
        lo = boots[int(0.025 * (bootstrap_n - 1))]
        hi = boots[int(0.975 * (bootstrap_n - 1))]
        return {
            "effect": name,
            "n_blocks": n,
            "mean_log_contrast": mean_c,
            "geometric_mean_ratio": math.exp(mean_c),
            "bootstrap_ci95_ratio": [lo, hi],
            "bootstrap_n": bootstrap_n,
            "bootstrap_seed": seed,
            "wilcoxon_two_sided_p": _wilcoxon_p(contrasts),
        }

    return {
        "metric": metric,
        "design": "randomized_complete_block_2x2",
        "primary_inference": True,
        "n_complete_blocks": len(complete),
        "block_ids": list(complete.keys()),
        "effects": {
            "width": _effect(width_c, "width_wide_over_narrow"),
            "population": _effect(pop_c, "population_1000_over_133"),
            "interaction": _effect(inter_c, "interaction_width_x_population"),
        },
        "anova_divergence_note": (
            "CRD two-way ANOVA pools residual variance across blocks and can "
            "inflate interaction significance when block-to-block correlation "
            "is present. Blocked log-contrasts respect the randomization unit."
        ),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--probe-n", type=int, default=10)
    p.add_argument("--cooldown-sec", type=float, default=0.5)
    p.add_argument("--timeout-sec", type=float, default=300.0)
    p.add_argument("--out-dir", type=Path, default=DATA)
    args = p.parse_args()

    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    runs_path = out_dir / "factorial_provisioning_runs.csv"
    summary_path = out_dir / "factorial_provisioning_summary.csv"
    manifest_path = out_dir / "factorial_provisioning_manifest.json"

    all_runs: list[dict[str, Any]] = []
    failures = 0
    schedules: list[list[str]] = []

    with runs_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=RUN_FIELDS)
        writer.writeheader()
        for repeat in range(1, args.repeats + 1):
            order = list(CELLS)
            random.Random(args.seed + repeat).shuffle(order)
            schedules.append([c["config_id"] for c in order])
            print(
                f"=== factorial repeat={repeat} order={[c['config_id'] for c in order]} ===",
                flush=True,
            )
            for idx, cell in enumerate(order, start=1):
                seed = args.seed + 1000 * repeat + ord(cell["config_id"])
                row = _run_cell(cell, seed, args.probe_n, args.timeout_sec)
                row["repeat"] = repeat
                row["order_in_block"] = idx
                writer.writerow(row)
                fh.flush()
                all_runs.append(row)
                status = "ok" if row["exit_code"] == 0 else f"FAIL:{row['error']}"
                print(
                    f"  {cell['config_id']}: pop={row['device_population_wall_sec']}s "
                    f"grant={row['probe_grant_mean_ms']}ms {status}",
                    flush=True,
                )
                if row["exit_code"] != 0:
                    failures += 1
                if args.cooldown_sec > 0:
                    time.sleep(args.cooldown_sec)

    with summary_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS)
        w.writeheader()
        w.writerows(_aggregate(all_runs))

    write_manifest(
        manifest_path,
        capture_manifest(
            seed=args.seed,
            n=args.repeats,
            warmup=0,
            script_paths=[
                LOAD_DIR / "factorial_provisioning_bench.py",
                LOAD_DIR / "common.py",
            ],
            extra={
                "experiment": "factorial_provisioning",
                "cells": CELLS,
                "schedules": schedules,
                "probe_n": args.probe_n,
                "failures": failures,
                "blocked_log_contrasts": _blocked_log_contrasts(
                    all_runs, "device_population_wall_sec", seed=args.seed
                ),
                "anova_crd_supplementary": _two_way_anova(
                    all_runs, "device_population_wall_sec"
                ),
                # Legacy key retained for older readers; not primary inference.
                "anova": _two_way_anova(all_runs, "device_population_wall_sec"),
                "outputs": {
                    "runs_csv": str(runs_path),
                    "summary_csv": str(summary_path),
                },
            },
        ),
    )
    print(f"Wrote {runs_path}\nWrote {summary_path}\nWrote {manifest_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
