#!/usr/bin/env python3
"""Generate article figures from campaign CSVs into ``artifacts/figures/<id>/``.

Design for paper layout:
  - one chart per file (no multi-panel figures);
  - no plot titles (captions belong in LaTeX);
  - legends outside the axes when present (no overlap).

Box plots are preferred; stacked bars only for composition.

Dispersion: boxplots show IQR/whiskers/fliers of the CSV row unit (lifecycle =
within-container cycles; cold start = independent containers). They are not
confidence intervals. ``persistence_wall_median`` bars are medians only, with
no error bars.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
REPO = Path(__file__).resolve().parents[1]
if sys.path and sys.path[0] == _SCRIPT_DIR:
    sys.path.pop(0)
sys.path.insert(0, str(REPO))

import statistics as st  # noqa: E402

from analysis.load_data import latest_campaign  # noqa: E402

OPS = [
    "registration",
    "spectrumInquiry",
    "grant",
    "heartbeat",
    "relinquishment",
    "deregistration",
]
OP_LABELS = {
    "registration": "Registration",
    "spectrumInquiry": "Spectrum Inquiry",
    "grant": "Grant",
    "heartbeat": "Heartbeat",
    "relinquishment": "Relinquishment",
    "deregistration": "Deregistration",
}
OP_FILE = {
    "registration": "registration",
    "spectrumInquiry": "spectrum_inquiry",
    "grant": "grant",
    "heartbeat": "heartbeat",
    "relinquishment": "relinquishment",
    "deregistration": "deregistration",
}
STAGE_KEYS = [
    ("stage_handler_ms", "Handler"),
    ("stage_database_ms", "Database"),
    ("stage_serialization_ms", "Serialization"),
    ("stage_other_ms", "Other"),
    ("external_ms", "External"),
]

C_BLUE = "#4C78A8"
C_ORANGE = "#F58518"
C_GREEN = "#54A24B"
C_PURPLE = "#B279A2"
C_RED = "#E45756"
C_TEAL = "#72B7B2"
C_GRAY = "#BAB0AC"
STAGE_COLORS = [C_BLUE, C_ORANGE, C_GREEN, C_PURPLE, C_RED]


def _median(vals: list[float]) -> float:
    return float(st.median(vals)) if vals else 0.0


def _short_profile(pid: str) -> str:
    return (
        pid.replace("synth_", "")
        .replace("protections_", "prot_")
        .replace("br_anatel_privativa", "anatel")
        .replace("cbrs_winnforum", "cbrs")
        .replace("lab_custom_3p8", "lab_3p8")
    )


def _style_ax(ax) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", linestyle=":", linewidth=0.6, alpha=0.7)
    ax.set_axisbelow(True)


def _boxplot(
    ax,
    data: list[list[float]],
    labels: list[str],
    *,
    ylabel: str,
    xlabel: str | None = None,
    colors: list[str] | None = None,
    rotate: int = 0,
    widths: float = 0.55,
) -> None:
    bp = ax.boxplot(
        data,
        tick_labels=labels,
        patch_artist=True,
        widths=widths,
        showfliers=True,
        flierprops={
            "marker": "o",
            "markersize": 3,
            "markerfacecolor": C_GRAY,
            "markeredgecolor": C_GRAY,
            "alpha": 0.7,
        },
        medianprops={"color": "#222222", "linewidth": 1.4},
        whiskerprops={"color": "#444444"},
        capprops={"color": "#444444"},
        boxprops={"linewidth": 0.9},
    )
    palette = colors or [C_BLUE] * len(data)
    for patch, color in zip(bp["boxes"], palette):
        patch.set_facecolor(color)
        patch.set_alpha(0.85)
    ax.set_ylabel(ylabel)
    if xlabel:
        ax.set_xlabel(xlabel)
    if rotate:
        ax.tick_params(axis="x", labelrotation=rotate)
    _style_ax(ax)


def _legend_outside(ax, ncol: int = 1) -> None:
    """Place legend to the right of the axes (never over the data)."""
    ax.legend(
        frameon=False,
        fontsize=8,
        loc="upper left",
        bbox_to_anchor=(1.02, 1.0),
        borderaxespad=0.0,
        ncol=ncol,
    )


def _save(fig, out: Path, stem: str, *, with_legend: bool = False) -> None:
    if with_legend:
        fig.savefig(out / f"{stem}.pdf", bbox_inches="tight")
        fig.savefig(out / f"{stem}.png", dpi=200, bbox_inches="tight")
    else:
        fig.tight_layout()
        fig.savefig(out / f"{stem}.pdf")
        fig.savefig(out / f"{stem}.png", dpi=200)
    import matplotlib.pyplot as plt

    plt.close(fig)


def _load_lifecycle(campaign: str) -> list[dict]:
    path = (
        REPO
        / "artifacts"
        / "raw"
        / campaign
        / "lifecycle_decomposition"
        / "lifecycle_decomp_raw.csv"
    )
    if not path.is_file():
        return []
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    return [r for r in rows if str(r.get("warmup")) in ("0", "false", "False", "")]


def _profiles_by_lifecycle(meas: list[dict]) -> list[str]:
    life = {
        p: _median(
            [
                float(r["lifecycle_total_ms"])
                for r in meas
                if r["profile_id"] == p and r["operation"] == "registration"
            ]
        )
        for p in {r["profile_id"] for r in meas}
    }
    return sorted(life.keys(), key=lambda p: life[p])


def _fig_lifecycle(meas: list[dict], out: Path) -> None:
    import matplotlib.pyplot as plt

    # E2E lifecycle by profile
    by_profile: dict[str, list[float]] = {}
    for r in meas:
        if r["operation"] != "registration":
            continue
        by_profile.setdefault(r["profile_id"], []).append(float(r["lifecycle_total_ms"]))
    order = sorted(by_profile.keys(), key=lambda p: _median(by_profile[p]))
    fig, ax = plt.subplots(figsize=(7.5, 4.0))
    _boxplot(
        ax,
        [by_profile[p] for p in order],
        [_short_profile(p) for p in order],
        ylabel="Lifecycle latency (ms)",
        xlabel="Band profile",
        colors=[C_RED if p == "synth_wide_20mhz" else C_BLUE for p in order],
        rotate=25,
    )
    _save(fig, out, "lifecycle_boxplot_by_profile")

    # Client latency by operation (all profiles pooled)
    op_colors = [C_BLUE, C_TEAL, C_ORANGE, C_PURPLE, C_GREEN, C_RED]
    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    _boxplot(
        ax,
        [[float(r["client_ms"]) for r in meas if r["operation"] == op] for op in OPS],
        [OP_LABELS[o] for o in OPS],
        ylabel="Client latency (ms)",
        xlabel="Operation",
        colors=op_colors[: len(OPS)],
        rotate=20,
    )
    _save(fig, out, "lifecycle_boxplot_by_operation")

    # One file per operation × profile
    profiles = _profiles_by_lifecycle(meas)
    for op in OPS:
        fig, ax = plt.subplots(figsize=(7.5, 4.0))
        _boxplot(
            ax,
            [
                [
                    float(r["client_ms"])
                    for r in meas
                    if r["profile_id"] == p and r["operation"] == op
                ]
                for p in profiles
            ],
            [_short_profile(p) for p in profiles],
            ylabel="Client latency (ms)",
            xlabel="Band profile",
            colors=[C_RED if p == "synth_wide_20mhz" else C_BLUE for p in profiles],
            rotate=25,
        )
        _save(fig, out, f"lifecycle_boxplot_{OP_FILE[op]}_by_profile")

    # One file per stage across operations
    for key, name, color in (
        ("stage_handler_ms", "handler", C_BLUE),
        ("stage_database_ms", "database", C_ORANGE),
        ("external_ms", "external", C_RED),
        ("stage_serialization_ms", "serialization", C_GREEN),
    ):
        fig, ax = plt.subplots(figsize=(7.2, 4.0))
        _boxplot(
            ax,
            [[float(r[key]) for r in meas if r["operation"] == op] for op in OPS],
            [OP_LABELS[o] for o in OPS],
            ylabel=f"{name.capitalize()} time (ms)",
            xlabel="Operation",
            colors=[color] * len(OPS),
            rotate=20,
        )
        _save(fig, out, f"lifecycle_boxplot_stage_{name}")

    # Stacked composition — single figure, legend outside
    labels = [OP_LABELS[o] for o in OPS]
    series = {name: [] for _, name in STAGE_KEYS}
    for op in OPS:
        rs = [r for r in meas if r["operation"] == op]
        for key, name in STAGE_KEYS:
            series[name].append(_median([float(r[key]) for r in rs]))
    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    bottoms = [0.0] * len(OPS)
    for (name, color) in zip([n for _, n in STAGE_KEYS], STAGE_COLORS):
        vals = series[name]
        ax.bar(labels, vals, bottom=bottoms, label=name, color=color, width=0.65)
        bottoms = [b + v for b, v in zip(bottoms, vals)]
    ax.set_ylabel("Median latency (ms)")
    ax.set_xlabel("Operation")
    ax.tick_params(axis="x", labelrotation=20)
    _style_ax(ax)
    _legend_outside(ax)
    _save(fig, out, "lifecycle_stacked_ops", with_legend=True)

    # Separate stacked files for CBRS and wide
    for pid, stem in (
        ("cbrs_winnforum", "lifecycle_stacked_cbrs"),
        ("synth_wide_20mhz", "lifecycle_stacked_wide"),
    ):
        fig, ax = plt.subplots(figsize=(7.2, 4.0))
        bottoms = [0.0] * len(OPS)
        for (key, name), color in zip(STAGE_KEYS, STAGE_COLORS):
            vals = []
            for op in OPS:
                rs = [
                    r
                    for r in meas
                    if r["operation"] == op and r["profile_id"] == pid
                ]
                vals.append(_median([float(r[key]) for r in rs]) if rs else 0.0)
            ax.bar(labels, vals, bottom=bottoms, label=name, color=color, width=0.65)
            bottoms = [b + v for b, v in zip(bottoms, vals)]
        ax.set_ylabel("Median latency (ms)")
        ax.set_xlabel("Operation")
        ax.tick_params(axis="x", labelrotation=20)
        _style_ax(ax)
        _legend_outside(ax)
        _save(fig, out, stem, with_legend=True)


def _fig_persistence(campaign: str, out: Path) -> None:
    import matplotlib.pyplot as plt
    import numpy as np

    path = (
        REPO
        / "artifacts"
        / "raw"
        / campaign
        / "persistence_decomposition"
        / "persistence_modes_runs.csv"
    )
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    modes = ["sqlite_commit", "sqlite_batch", "sqlite_memory"]
    mode_labels = {
        "sqlite_commit": "Commit",
        "sqlite_batch": "Batch",
        "sqlite_memory": "Memory",
    }
    pops = sorted({int(r["devices"]) for r in rows})
    colors = [C_BLUE, C_ORANGE, C_GREEN]

    for n in pops:
        fig, ax = plt.subplots(figsize=(5.5, 4.0))
        data = [
            [
                float(r["population_wall_sec"])
                for r in rows
                if r["persist_mode"] == mode and int(r["devices"]) == n
            ]
            for mode in modes
        ]
        _boxplot(
            ax,
            data,
            [mode_labels[m] for m in modes],
            ylabel="Provisioning time (s)",
            xlabel="Persistence mode",
            colors=colors,
        )
        _save(fig, out, f"persistence_boxplot_{n}_devices")

    # Companion grouped bars — legend outside
    x = np.arange(len(pops))
    width = 0.25
    fig, ax = plt.subplots(figsize=(6.5, 4.0))
    for i, mode in enumerate(modes):
        vals = []
        for n in pops:
            subset = [
                float(r["population_wall_sec"])
                for r in rows
                if r["persist_mode"] == mode and int(r["devices"]) == n
            ]
            vals.append(_median(subset))
        ax.bar(
            x + (i - 1) * width,
            vals,
            width,
            label=mode_labels[mode],
            color=colors[i],
        )
    ax.set_xticks(x)
    ax.set_xticklabels([str(n) for n in pops])
    ax.set_xlabel("Devices")
    ax.set_ylabel("Median provisioning time (s)")
    _style_ax(ax)
    _legend_outside(ax)
    _save(fig, out, "persistence_wall_median", with_legend=True)


def _fig_coldstart(campaign: str, out: Path) -> None:
    import matplotlib.pyplot as plt

    path = (
        REPO
        / "artifacts"
        / "raw"
        / campaign
        / "instrumented_cold_start"
        / "unified_coldstart_runs.csv"
    )
    rows = list(csv.DictReader(path.open(encoding="utf-8")))

    ext_keys = [
        ("cold_start_total_ms", "Total"),
        ("docker_run_to_container_started_ms", "Docker to start"),
        ("internal_total_ms", "Internal"),
        ("runtime_and_observation_gap_ms", "Runtime gap"),
    ]
    fig, ax = plt.subplots(figsize=(6.8, 4.0))
    _boxplot(
        ax,
        [[float(r[k]) for r in rows] for k, _ in ext_keys],
        [lab for _, lab in ext_keys],
        ylabel="Time (ms)",
        colors=[C_PURPLE, C_BLUE, C_ORANGE, C_RED],
        rotate=15,
    )
    _save(fig, out, "coldstart_boxplot_external")

    int_keys = [
        ("python_bootstrap_ms", "Bootstrap"),
        ("profile_load_ms", "Profile"),
        ("database_init_ms", "Database"),
        ("tls_init_ms", "TLS"),
        ("internal_other_ms", "Other"),
    ]
    fig, ax = plt.subplots(figsize=(6.8, 4.0))
    _boxplot(
        ax,
        [[float(r[k]) for r in rows] for k, _ in int_keys],
        [lab for _, lab in int_keys],
        ylabel="Time (ms)",
        colors=[C_BLUE, C_TEAL, C_ORANGE, C_PURPLE, C_GRAY],
        rotate=15,
    )
    _save(fig, out, "coldstart_boxplot_internal")

    profiles = sorted(
        {r["profile_id"] for r in rows},
        key=lambda p: _median(
            [float(r["cold_start_total_ms"]) for r in rows if r["profile_id"] == p]
        ),
    )
    fig, ax = plt.subplots(figsize=(7.5, 4.0))
    _boxplot(
        ax,
        [
            [float(r["cold_start_total_ms"]) for r in rows if r["profile_id"] == p]
            for p in profiles
        ],
        [_short_profile(p) for p in profiles],
        ylabel="Cold-start total (ms)",
        xlabel="Band profile",
        colors=[C_BLUE] * len(profiles),
        rotate=25,
    )
    _save(fig, out, "coldstart_boxplot_by_profile")


def _fig_mtls(campaign: str, out: Path) -> None:
    import matplotlib.pyplot as plt

    path = (
        REPO
        / "artifacts"
        / "raw"
        / campaign
        / "mtls_correctness_overhead"
        / "tls_mtls_requests.csv"
    )
    rows = [
        r
        for r in csv.DictReader(path.open(encoding="utf-8"))
        if str(r.get("warmup")) in ("0", "false", "False", "")
    ]

    # Combined (may compress persistent scale — keep for overview)
    conditions = ["tls_new", "mtls_new", "tls_persistent", "mtls_persistent"]
    labels = ["TLS new", "mTLS new", "TLS persist.", "mTLS persist."]
    fig, ax = plt.subplots(figsize=(6.8, 4.0))
    _boxplot(
        ax,
        [[float(r["total_ms"]) for r in rows if r["condition"] == c] for c in conditions],
        labels,
        ylabel="Request latency (ms)",
        colors=[C_BLUE, C_RED, C_TEAL, C_ORANGE],
        rotate=15,
    )
    _save(fig, out, "mtls_boxplot_all")

    # Separate scales — no subplots
    for pair, labs, cols, stem in (
        (
            ("tls_new", "mtls_new"),
            ["TLS", "mTLS"],
            [C_BLUE, C_RED],
            "mtls_boxplot_new_connection",
        ),
        (
            ("tls_persistent", "mtls_persistent"),
            ["TLS", "mTLS"],
            [C_TEAL, C_ORANGE],
            "mtls_boxplot_persistent",
        ),
    ):
        fig, ax = plt.subplots(figsize=(4.8, 4.0))
        _boxplot(
            ax,
            [[float(r["total_ms"]) for r in rows if r["condition"] == c] for c in pair],
            labs,
            ylabel="Request latency (ms)",
            colors=list(cols),
            widths=0.45,
        )
        _save(fig, out, stem)


def _write_mtls_table(campaign: str, out: Path) -> None:
    path = (
        REPO
        / "artifacts"
        / "raw"
        / campaign
        / "mtls_correctness_overhead"
        / "tls_mtls_overhead_summary.csv"
    )
    if not path.is_file():
        return
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    med: dict[str, float] = {}
    for r in rows:
        if r.get("metric") == "latency_median_ms":
            med[r["condition"]] = float(r["median"])

    pairs = [
        ("New", "tls_new", "mtls_new"),
        ("Persistent", "tls_persistent", "mtls_persistent"),
    ]
    table_rows = []
    for conn, tls_k, mtls_k in pairs:
        if tls_k not in med or mtls_k not in med:
            continue
        t, m = med[tls_k], med[mtls_k]
        oh = m - t
        table_rows.append(
            {
                "connection": conn,
                "tls_ms": t,
                "mtls_ms": m,
                "absolute_overhead_ms": oh,
                "relative_overhead_pct": 100.0 * oh / t if t else 0.0,
            }
        )
    if not table_rows:
        return

    with (out / "mtls_overhead_compact.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(table_rows[0].keys()))
        w.writeheader()
        w.writerows(table_rows)

    lines = [
        "# mTLS overhead (compact)",
        "",
        "| Connection | TLS | mTLS | Absolute overhead | Relative overhead |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for r in table_rows:
        lines.append(
            f"| {r['connection']} | {r['tls_ms']:.4g} ms | {r['mtls_ms']:.4g} ms | "
            f"{r['absolute_overhead_ms']:.4g} ms | {r['relative_overhead_pct']:.2f}% |"
        )
    lines.append("")
    (out / "mtls_overhead_compact.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--campaign-id", default=None)
    args = p.parse_args()
    campaign = args.campaign_id or latest_campaign()
    if not campaign:
        print("no campaign under artifacts/raw/", file=sys.stderr)
        return 1

    try:
        import matplotlib

        matplotlib.use("Agg")
    except ImportError:
        print("matplotlib required: pip install matplotlib", file=sys.stderr)
        return 1

    out = REPO / "artifacts" / "figures" / campaign
    out.mkdir(parents=True, exist_ok=True)

    # Drop superseded multi-panel / titled companions if present
    obsolete = [
        "lifecycle_boxplot_op_x_profile",
        "lifecycle_boxplot_stages",
        "lifecycle_stacked_cbrs_vs_wide",
        "lifecycle_by_profile",
        "coldstart_boxplot",
        "coldstart_decomposition",
        "mtls_boxplot",
        "mtls_boxplot_panels",
        "persistence_boxplot",
        "persistence_wall",
    ]
    for stem in obsolete:
        for ext in (".png", ".pdf"):
            p = out / f"{stem}{ext}"
            if p.is_file():
                p.unlink()

    meas = _load_lifecycle(campaign)
    if meas:
        _fig_lifecycle(meas, out)
    else:
        print("skip lifecycle figures: no measured rows", flush=True)

    pers = (
        REPO
        / "artifacts"
        / "raw"
        / campaign
        / "persistence_decomposition"
        / "persistence_modes_runs.csv"
    )
    if pers.is_file():
        _fig_persistence(campaign, out)
    else:
        print("skip persistence figures: missing runs csv", flush=True)

    cold = (
        REPO
        / "artifacts"
        / "raw"
        / campaign
        / "instrumented_cold_start"
        / "unified_coldstart_runs.csv"
    )
    if cold.is_file():
        _fig_coldstart(campaign, out)
    else:
        print("skip cold-start figures: missing runs csv", flush=True)

    mtls_req = (
        REPO
        / "artifacts"
        / "raw"
        / campaign
        / "mtls_correctness_overhead"
        / "tls_mtls_requests.csv"
    )
    if mtls_req.is_file():
        _fig_mtls(campaign, out)
    else:
        print("skip mTLS figures: missing requests csv", flush=True)

    _write_mtls_table(campaign, out)

    readme = out / "README.md"
    readme.write_text(
        f"# Figures — `{campaign}`\n\n"
        "Rules: **one chart per file**, **no titles**, legends **outside** axes.\n\n"
        "## Lifecycle\n"
        "- `lifecycle_boxplot_by_profile`\n"
        "- `lifecycle_boxplot_by_operation`\n"
        "- `lifecycle_boxplot_<op>_by_profile` (6 files)\n"
        "- `lifecycle_boxplot_stage_{handler,database,external,serialization}`\n"
        "- `lifecycle_stacked_ops`, `lifecycle_stacked_cbrs`, `lifecycle_stacked_wide`\n\n"
        "## Persistence / cold start / mTLS\n"
        "- `persistence_boxplot_<N>_devices`, `persistence_wall_median`\n"
        "- `coldstart_boxplot_{external,internal,by_profile}`\n"
        "- `mtls_boxplot_{all,new_connection,persistent}`\n"
        "- `mtls_overhead_compact.(csv|md)`\n",
        encoding="utf-8",
    )
    print(f"Wrote figures under {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
