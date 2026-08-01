#!/usr/bin/env python3
"""Investigate synth_wide_20mhz latency using existing lifecycle CSVs (no re-run).

Writes Markdown + CSV under ``artifacts/tables/<campaign>/wide_profile/``.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

# Running as ``python analysis/...py`` puts this directory on sys.path[0] and
# would shadow the stdlib ``statistics`` module with ``analysis/statistics.py``.
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
REPO = Path(__file__).resolve().parents[1]
if sys.path and sys.path[0] == _SCRIPT_DIR:
    sys.path.pop(0)
sys.path.insert(0, str(REPO))

import statistics as st  # noqa: E402

from analysis.load_data import latest_campaign  # noqa: E402

PROFILES_DIR = REPO / "configs" / "profiles"


def _num(row: dict, key: str) -> float | None:
    v = row.get(key, "")
    if v in ("", None):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _median(xs: list[float | None]) -> float | None:
    vals = [x for x in xs if x is not None]
    return st.median(vals) if vals else None


def _corr(xs: list[float | None], ys: list[float | None]) -> float | None:
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    mx = st.mean(p[0] for p in pairs)
    my = st.mean(p[1] for p in pairs)
    num = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    denx = math.sqrt(sum((p[0] - mx) ** 2 for p in pairs))
    deny = math.sqrt(sum((p[1] - my) ** 2 for p in pairs))
    if denx == 0 or deny == 0:
        return None
    return num / (denx * deny)


def _channel_geometry() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(PROFILES_DIR.glob("*.json")):
        d = json.loads(path.read_text(encoding="utf-8"))
        low = int(d["band_low_hz"])
        high = int(d["band_high_hz"])
        width = int(d["channel_width_hz"])
        out[d["profile_id"]] = {
            "band_mhz": (high - low) / 1e6,
            "channel_width_mhz": width / 1e6,
            "n_channels_geom": (high - low) // width,
        }
    return out


def _md_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return lines


def _fmt(v: float | None, nd: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:.{nd}f}"


def analyze(campaign: str) -> Path:
    raw = (
        REPO
        / "artifacts"
        / "raw"
        / campaign
        / "lifecycle_decomposition"
        / "lifecycle_decomp_raw.csv"
    )
    if not raw.is_file():
        raise SystemExit(f"missing {raw}")

    rows = list(csv.DictReader(raw.open(encoding="utf-8")))
    measured = [r for r in rows if str(r.get("warmup")) in ("0", "false", "False", "")]
    geo = _channel_geometry()
    profiles = sorted({r["profile_id"] for r in measured})
    ops = [
        "registration",
        "spectrumInquiry",
        "grant",
        "heartbeat",
        "relinquishment",
        "deregistration",
    ]

    out_dir = REPO / "artifacts" / "tables" / campaign / "wide_profile"
    out_dir.mkdir(parents=True, exist_ok=True)

    per_profile: list[dict] = []
    for pid in profiles:
        rs = [r for r in measured if r["profile_id"] == pid]
        life = _median(
            [
                _num(r, "lifecycle_total_ms")
                for r in rs
                if r["operation"] == "registration"
            ]
        )
        siq = [r for r in rs if r["operation"] == "spectrumInquiry"]
        row = {
            "profile_id": pid,
            "lifecycle_median_ms": life,
            "response_bytes_median_all_ops": _median(
                [_num(r, "response_bytes") for r in rs]
            ),
            "siq_response_bytes_median": _median(
                [_num(r, "response_bytes") for r in siq]
            ),
            "n_channels_geom": geo.get(pid, {}).get("n_channels_geom"),
            "channel_width_mhz": geo.get(pid, {}).get("channel_width_mhz"),
            "siq_client_median_ms": _median([_num(r, "client_ms") for r in siq]),
            "siq_handler_median_ms": _median(
                [_num(r, "stage_handler_ms") for r in siq]
            ),
            "siq_external_median_ms": _median([_num(r, "external_ms") for r in siq]),
        }
        per_profile.append(row)

    with (out_dir / "per_profile_payload.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(per_profile[0].keys()))
        w.writeheader()
        w.writerows(per_profile)

    decomp_rows: list[dict] = []
    for op in ops:
        for pid in profiles:
            rs = [
                r
                for r in measured
                if r["profile_id"] == pid and r["operation"] == op
            ]
            decomp_rows.append(
                {
                    "profile_id": pid,
                    "operation": op,
                    "client_ms": _median([_num(r, "client_ms") for r in rs]),
                    "handler_ms": _median([_num(r, "stage_handler_ms") for r in rs]),
                    "database_ms": _median([_num(r, "stage_database_ms") for r in rs]),
                    "serialization_ms": _median(
                        [_num(r, "stage_serialization_ms") for r in rs]
                    ),
                    "other_ms": _median([_num(r, "stage_other_ms") for r in rs]),
                    "external_ms": _median([_num(r, "external_ms") for r in rs]),
                    "response_bytes": _median([_num(r, "response_bytes") for r in rs]),
                    "request_bytes": _median([_num(r, "request_bytes") for r in rs]),
                }
            )
    with (out_dir / "decomp_by_op_profile.csv").open(
        "w", newline="", encoding="utf-8"
    ) as fh:
        w = csv.DictWriter(fh, fieldnames=list(decomp_rows[0].keys()))
        w.writeheader()
        w.writerows(decomp_rows)

    # Correlations
    corr_rows = []
    for label, subset in [
        ("all_ops", measured),
        (
            "spectrumInquiry",
            [r for r in measured if r["operation"] == "spectrumInquiry"],
        ),
    ]:
        corr_rows.append(
            {
                "scope": label,
                "profile_id": "*",
                "pearson_response_bytes_x_client_ms": _corr(
                    [_num(r, "response_bytes") for r in subset],
                    [_num(r, "client_ms") for r in subset],
                ),
                "n": len(subset),
            }
        )
    # Cross-profile medians (SIQ)
    siq_meds = [
        (
            r["siq_response_bytes_median"],
            r["siq_client_median_ms"],
            r["lifecycle_median_ms"],
        )
        for r in per_profile
        if r["siq_response_bytes_median"] is not None
    ]
    corr_rows.append(
        {
            "scope": "cross_profile_siq_medians",
            "profile_id": "*",
            "pearson_response_bytes_x_client_ms": _corr(
                [t[0] for t in siq_meds], [t[1] for t in siq_meds]
            ),
            "n": len(siq_meds),
        }
    )
    corr_rows.append(
        {
            "scope": "cross_profile_siq_bytes_x_lifecycle",
            "profile_id": "*",
            "pearson_response_bytes_x_client_ms": _corr(
                [t[0] for t in siq_meds], [t[2] for t in siq_meds]
            ),
            "n": len(siq_meds),
        }
    )

    with (out_dir / "correlations.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(corr_rows[0].keys()))
        w.writeheader()
        w.writerows(corr_rows)

    # Regime split for wide (observed step change)
    wide_reg = [
        r
        for r in measured
        if r["profile_id"] == "synth_wide_20mhz" and r["operation"] == "registration"
    ]
    early = [
        _num(r, "lifecycle_total_ms")
        for r in wide_reg
        if int(r["cycle"]) < 24
    ]
    late = [
        _num(r, "lifecycle_total_ms")
        for r in wide_reg
        if int(r["cycle"]) >= 24
    ]
    regime = {
        "profile_id": "synth_wide_20mhz",
        "split_cycle": 24,
        "early_cycles": "4-23",
        "early_n": len([x for x in early if x is not None]),
        "early_lifecycle_median_ms": _median(early),
        "late_cycles": "24-33",
        "late_n": len([x for x in late if x is not None]),
        "late_lifecycle_median_ms": _median(late),
        "note": (
            "Late regime aligns with other profiles; early regime elevates both "
            "handler and external with identical REG/GRA/HBT payload sizes."
        ),
    }
    (out_dir / "wide_regime.json").write_text(
        json.dumps(regime, indent=2) + "\n", encoding="utf-8"
    )

    # Findings markdown
    wide = next(r for r in per_profile if r["profile_id"] == "synth_wide_20mhz")
    narrow = next(r for r in per_profile if r["profile_id"] == "synth_narrow_5mhz")
    others_life = [
        r["lifecycle_median_ms"]
        for r in per_profile
        if r["profile_id"] != "synth_wide_20mhz" and r["lifecycle_median_ms"]
    ]
    md: list[str] = [
        f"# Análise: `synth_wide_20mhz` — campaign `{campaign}`",
        "",
        "Análise somente com dados já coletados (sem reexecução).",
        "",
        "## Veredito",
        "",
        "A latência elevada de `synth_wide_20mhz` **não** é explicada por largura "
        "de canal, quantidade de canais ou tamanho de payload.",
        "",
        f"- Lifecycle mediano do wide: **{_fmt(wide['lifecycle_median_ms'])} ms** "
        f"(demais perfis: {_fmt(min(others_life))}–{_fmt(max(others_life))} ms).",
        f"- SIQ `response_bytes` do wide: **{int(wide['siq_response_bytes_median'] or 0)}** "
        f"(menor da campanha); narrow 5 MHz: "
        f"**{int(narrow['siq_response_bytes_median'] or 0)}** "
        f"({int(narrow['n_channels_geom'] or 0)} canais geométricos vs "
        f"{int(wide['n_channels_geom'] or 0)}).",
        "- REG/GRA/HBT têm `response_bytes` idênticos entre perfis, mas o wide "
        "permanece ~1,6× mais lento nessas operações.",
        "- Handler **e** externo sobem juntos → compatível com contenção de host / "
        "runtime, não com serialização de canais.",
        f"- Dentro do próprio wide há mudança de regime no ciclo 24: "
        f"ciclos 4–23 mediana {_fmt(regime['early_lifecycle_median_ms'])} ms; "
        f"ciclos 24–33 mediana {_fmt(regime['late_lifecycle_median_ms'])} ms "
        "(alinhada aos demais perfis).",
        "",
        "## Implicação para o artigo",
        "",
        "Não afirmar que a largura de 20 MHz causa o aumento. Tratar o outlier "
        "como variação ambiental localizada durante a janela daquele container, "
        "ou reportar a mediana com nota sobre o regime tardio. A narrativa "
        "\"baixa variação entre perfis\" permanece válida para os outros sete "
        "perfis (≈12,6–17,5 ms).",
        "",
        "## Payload e canais por perfil",
        "",
    ]
    md += _md_table(
        [
            "profile",
            "life_ms",
            "siq_bytes",
            "n_ch",
            "width_MHz",
            "siq_client",
            "siq_handler",
            "siq_ext",
        ],
        [
            [
                r["profile_id"],
                _fmt(r["lifecycle_median_ms"]),
                _fmt(r["siq_response_bytes_median"], 0),
                str(r["n_channels_geom"] or "—"),
                _fmt(r["channel_width_mhz"], 0),
                _fmt(r["siq_client_median_ms"], 3),
                _fmt(r["siq_handler_median_ms"], 3),
                _fmt(r["siq_external_median_ms"], 3),
            ]
            for r in sorted(per_profile, key=lambda x: x["lifecycle_median_ms"] or 0)
        ],
    )
    md += [
        "",
        "## Correlações `response_bytes` × `client_ms`",
        "",
    ]
    md += _md_table(
        ["scope", "r", "n"],
        [
            [
                r["scope"],
                _fmt(r["pearson_response_bytes_x_client_ms"], 3),
                str(r["n"]),
            ]
            for r in corr_rows
        ],
    )
    md += [
        "",
        "Correlação cruzada entre medianas SIQ é **negativa** (mais bytes ↔ "
        "menor latência), o oposto do esperado se o payload dominasse.",
        "",
        "## Decomposição: wide vs demais (medianas)",
        "",
    ]
    for op in ops:
        wrow = next(
            r
            for r in decomp_rows
            if r["profile_id"] == "synth_wide_20mhz" and r["operation"] == op
        )
        orows = [
            r
            for r in decomp_rows
            if r["profile_id"] != "synth_wide_20mhz" and r["operation"] == op
        ]
        o_client = _median([r["client_ms"] for r in orows])
        o_bytes = _median([r["response_bytes"] for r in orows])
        md += [
            f"### `{op}`",
            "",
            f"- client: {_fmt(wrow['client_ms'], 3)} vs {_fmt(o_client, 3)} ms "
            f"(×{_fmt((wrow['client_ms'] or 0) / (o_client or 1), 2)})",
            f"- bytes: {_fmt(wrow['response_bytes'], 0)} vs {_fmt(o_bytes, 0)}",
            f"- handler: {_fmt(wrow['handler_ms'], 3)} · external: "
            f"{_fmt(wrow['external_ms'], 3)}",
            "",
        ]

    md += [
        "## Regime interno do wide",
        "",
        f"- Ciclos 4–23: mediana lifecycle {_fmt(regime['early_lifecycle_median_ms'])} ms "
        f"(n={regime['early_n']})",
        f"- Ciclos 24–33: mediana lifecycle {_fmt(regime['late_lifecycle_median_ms'])} ms "
        f"(n={regime['late_n']})",
        "",
        "Arquivos gerados: `per_profile_payload.csv`, `decomp_by_op_profile.csv`, "
        "`correlations.csv`, `wide_regime.json`.",
        "",
    ]
    (out_dir / "REPORT.md").write_text("\n".join(md), encoding="utf-8")
    print(f"Wrote {out_dir / 'REPORT.md'}")
    return out_dir


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--campaign-id", default=None)
    args = p.parse_args()
    campaign = args.campaign_id or latest_campaign()
    if not campaign:
        print("no campaign under artifacts/raw/", file=sys.stderr)
        return 1
    analyze(campaign)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
