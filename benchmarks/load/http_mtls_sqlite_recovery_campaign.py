"""Independent HTTP/mTLS SQLite recovery cycles (definitive campaign helper).

Each cycle: start → Reg/Grant/HB → SIGKILL → restart same DB → /ready → HB rc=0.

Does **not** call process_heartbeat() in-process. Does **not** claim power-loss.
Does **not** copy certificates or private keys into --out-dir.

Writes per-cycle JSON + summary CSV under --out-dir. Resumable via existing
cycle_*.json / cycles_done.txt without duplicating summary rows.

Failure classes
---------------
* ``infrastructure`` — docker/ready/TLS setup failures (may justify invalidation).
* ``measured_sas`` — HTTP/SAS response failures after the service was ready
  (always retained; never selectively dropped).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from common import (
    build_mtls_context,
    cert_sha256_fingerprint,
    image_digest,
    wait_http_200,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IMAGE = os.environ.get("SAS_RECOVERY_IMAGE", "sas-agent:article")
DEFAULT_CERTS = [
    Path(os.environ["HARNESS_CERTS_DIR"]) if os.environ.get("HARNESS_CERTS_DIR") else None,
    ROOT / "evidence/tnsm-2026-12135-controlled-20261111T205011Z/meta/harness_certs",
    ROOT.parent / "src" / "harness" / "certs",
]
SUMMARY_FIELDS = [
    "cycle",
    "ok",
    "failure_class",
    "dt_sigkill_to_ready2_mono_s",
    "dt_sigkill_to_first_https_sas_mono_s",
    "dt_ready2_to_first_https_sas_mono_s",
    "db_bytes_before_kill",
    "error",
]


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _mono() -> float:
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def _run(cmd: list[str], timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _certs_dir(explicit: Path | None) -> Path:
    if explicit is not None:
        p = explicit.expanduser().resolve()
        if not (p / "ca.cert").is_file():
            raise SystemExit(f"certs missing under {p}")
        return p
    for c in DEFAULT_CERTS:
        if c and (c / "ca.cert").is_file():
            return c.resolve()
    raise SystemExit("harness certs not found; set --certs-dir or HARNESS_CERTS_DIR")


def _classify_error(msg: str) -> str:
    low = msg.lower()
    infra_tokens = (
        "docker",
        "ready failed",
        "ssl",
        "certificate",
        "connection refused",
        "connecterror",
        "timeout",
        "name resolution",
        "no such container",
    )
    if any(t in low for t in infra_tokens):
        return "infrastructure"
    return "measured_sas"


def _load_done(cycles_dir: Path, done_path: Path) -> set[int]:
    done: set[int] = set()
    if done_path.is_file():
        done |= {int(x) for x in done_path.read_text().split() if x.strip().isdigit()}
    for path in cycles_dir.glob("cycle_*.json"):
        try:
            cycle = int(path.stem.split("_", 1)[1])
        except (IndexError, ValueError):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # Completed attempt (success or recorded failure) — do not re-run.
        if "ok" in payload:
            done.add(cycle)
    return done


def _summary_cycles(summary_path: Path) -> set[int]:
    if not summary_path.is_file():
        return set()
    out: set[int] = set()
    with summary_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                out.add(int(row["cycle"]))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def _one_cycle(
    *,
    cycle: int,
    image: str,
    certs: Path,
    host_port: int,
    name: str,
    out_cycle: Path,
) -> dict:
    ctx = build_mtls_context(certs)
    tmp = tempfile.mkdtemp(prefix=f"sas_http_rec_c{cycle:02d}_")
    data_dir = Path(tmp) / "data"
    data_dir.mkdir()
    db_host = data_dir / "recovery.db"
    timeline: dict = {
        "cycle": cycle,
        "clock": "CLOCK_MONOTONIC+UTC",
        "events": [],
        "ok": False,
        "failure_class": "",
        "artifacts_exclude": ["*.key", "private cert PEM material", "harness_certs copy"],
    }

    def mark(ev: str, **extra: object) -> None:
        timeline["events"].append(
            {"name": ev, "mono_s": _mono(), "wall_utc": _utc(), **extra}
        )

    def start() -> None:
        admin_fp = cert_sha256_fingerprint(certs / "domain_proxy.cert")
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"{host_port}:9000",
            "-v",
            f"{certs}:/app/src/harness/certs:ro",
            "-v",
            f"{data_dir.resolve()}:/data",
            "-e",
            "SAS_DATABASE_PATH=/data/recovery.db",
            "-e",
            "BAND_PROFILE=cbrs_winnforum",
            "-e",
            f"SAS_ADMIN_CERT_SHA256={admin_fp}",
            image,
        ]
        proc = _run(cmd)
        if proc.returncode != 0:
            raise RuntimeError(f"docker run failed: {proc.stderr.strip()}")

    def base() -> str:
        return f"https://127.0.0.1:{host_port}"

    def wait_ready() -> int:
        ok, polls, err = wait_http_200(f"{base()}/ready", ctx, 90.0)
        if not ok:
            raise RuntimeError(f"ready failed: {err}")
        mark("http_ready", polls=polls)
        return polls

    # Only this campaign container name.
    _run(["docker", "rm", "-f", name])
    try:
        mark("start_container_1")
        start()
        wait_ready()
        with httpx.Client(verify=ctx, timeout=30.0) as client:
            client.post(
                f"{base()}/admin/injectdata/user_id", json={"userId": "rec_user"}
            ).raise_for_status()
            client.post(
                f"{base()}/admin/injectdata/fcc_id",
                json={"fccId": "rec_fcc", "fccMaxEirp": 47},
            ).raise_for_status()
            sn = f"rec_sn_{cycle:04d}"
            reg = client.post(
                f"{base()}/v1.2/registration",
                json={
                    "registrationRequest": [
                        {
                            "userId": "rec_user",
                            "fccId": "rec_fcc",
                            "cbsdSerialNumber": sn,
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
                    ]
                },
            )
            reg.raise_for_status()
            cbsd_id = reg.json()["registrationResponse"][0]["cbsdId"]
            if reg.json()["registrationResponse"][0]["response"]["responseCode"] != 0:
                raise RuntimeError(f"registration failed: {reg.text}")
            gra = client.post(
                f"{base()}/v1.2/grant",
                json={
                    "grantRequest": [
                        {
                            "cbsdId": cbsd_id,
                            "operationParam": {
                                "maxEirp": 20.0,
                                "operationFrequencyRange": {
                                    "lowFrequency": 3_550_000_000,
                                    "highFrequency": 3_560_000_000,
                                },
                            },
                        }
                    ]
                },
            )
            gra.raise_for_status()
            g0 = gra.json()["grantResponse"][0]
            if g0["response"]["responseCode"] != 0:
                raise RuntimeError(f"grant failed: {gra.text}")
            grant_id = g0["grantId"]
            hb1 = client.post(
                f"{base()}/v1.2/heartbeat",
                json={
                    "heartbeatRequest": [
                        {
                            "cbsdId": cbsd_id,
                            "grantId": grant_id,
                            "operationState": "GRANTED",
                        }
                    ]
                },
            )
            hb1.raise_for_status()
            if hb1.json()["heartbeatResponse"][0]["response"]["responseCode"] != 0:
                raise RuntimeError(f"pre-kill HB failed: {hb1.text}")
            mark("pre_kill_heartbeat_ok", cbsd_id=cbsd_id, grant_id=grant_id)

        if not db_host.is_file() or db_host.stat().st_size <= 0:
            raise RuntimeError("SQLite file missing after commit path")
        db_size = db_host.stat().st_size

        mark("sigkill")
        kill = _run(["docker", "kill", "--signal", "SIGKILL", name])
        if kill.returncode != 0:
            raise RuntimeError(f"SIGKILL failed: {kill.stderr.strip()}")
        _run(["docker", "rm", "-f", name])
        mark("container_removed")

        mark("start_container_2")
        start()
        wait_ready()
        with httpx.Client(verify=ctx, timeout=30.0) as client:
            hb2 = client.post(
                f"{base()}/v1.2/heartbeat",
                json={
                    "heartbeatRequest": [
                        {
                            "cbsdId": cbsd_id,
                            "grantId": grant_id,
                            "operationState": "AUTHORIZED",
                        }
                    ]
                },
            )
            mark(
                "first_https_heartbeat_after_restart",
                http_status=hb2.status_code,
            )
            if hb2.status_code != 200:
                raise RuntimeError(
                    f"post-restart HB HTTP {hb2.status_code}: {hb2.text}"
                )
            body = hb2.json()["heartbeatResponse"][0]
            if body["response"]["responseCode"] != 0:
                raise RuntimeError(f"post-restart HB rc!=0: {body}")

        ready_events = [e for e in timeline["events"] if e["name"] == "http_ready"]
        by_name = {e["name"]: e for e in timeline["events"]}
        t_kill = float(by_name["sigkill"]["mono_s"])
        t_ready2 = float(ready_events[-1]["mono_s"])
        t_hb2 = float(by_name["first_https_heartbeat_after_restart"]["mono_s"])
        timeline.update(
            {
                "ok": True,
                "failure_class": "",
                "db_bytes_before_kill": db_size,
                "cbsd_id": cbsd_id,
                "grant_id": grant_id,
                "dt_sigkill_to_ready2_mono_s": t_ready2 - t_kill,
                "dt_sigkill_to_first_https_sas_mono_s": t_hb2 - t_kill,
                "dt_ready2_to_first_https_sas_mono_s": t_hb2 - t_ready2,
                "first_valid_sas_op": "https_heartbeat_responseCode_0",
                "not_claimed": ["power_loss", "media_failure", "statistical_sla"],
            }
        )
    except Exception as exc:  # noqa: BLE001 — campaign records failures
        err = f"{type(exc).__name__}: {exc}"
        timeline["ok"] = False
        timeline["error"] = err
        timeline["failure_class"] = _classify_error(err)
    finally:
        _run(["docker", "rm", "-f", name])
        shutil.rmtree(tmp, ignore_errors=True)

    out_cycle.write_text(json.dumps(timeline, indent=2) + "\n", encoding="utf-8")
    return timeline


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cycles", type=int, default=30)
    p.add_argument("--start-cycle", type=int, default=1, help="1-based; for resume")
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--image", default=DEFAULT_IMAGE)
    p.add_argument("--certs-dir", type=Path, default=None)
    p.add_argument("--host-port", type=int, default=19991)
    p.add_argument("--container-name", default="sas-http-sqlite-recovery-camp")
    p.add_argument("--host-commit", default="")
    p.add_argument("--image-id", default="", help="Immutable docker image Id (sha256:…)")
    args = p.parse_args()

    out = args.out_dir.expanduser().resolve()
    if "2026-12135/" in str(out).replace("\\", "/") and str(out).endswith(
        "evidence/2026-12135"
    ):
        raise SystemExit("refusing to write into canonical evidence/2026-12135")
    out.mkdir(parents=True, exist_ok=True)
    cycles_dir = out / "cycles"
    cycles_dir.mkdir(exist_ok=True)
    done_path = out / "cycles_done.txt"
    summary_path = out / "recovery_cycles_summary.csv"

    done = _load_done(cycles_dir, done_path)
    already_in_summary = _summary_cycles(summary_path)

    certs = _certs_dir(args.certs_dir)
    digest = image_digest(args.image)
    img_id = args.image_id.strip() or subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", args.image],
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()

    script_path = Path(__file__).resolve()
    manifest = {
        "created_at_utc": _utc(),
        "experiment": "http_mtls_sqlite_recovery",
        "cycles_planned": args.cycles,
        "start_cycle": args.start_cycle,
        "image_tag": args.image,
        "image_id": img_id,
        "image_repo_digest": digest,
        "host_commit": args.host_commit,
        "certs_dir": str(certs),
        "certs_copied_into_out": False,
        "script": str(script_path),
        "script_sha256": _sha256_file(script_path),
        "clock": "CLOCK_MONOTONIC+UTC",
        "failure_taxonomy": {
            "infrastructure": "docker/ready/TLS setup — may invalidate a cycle",
            "measured_sas": "SAS/HTTP after ready — retained; never selectively dropped",
        },
        "not_claimed": [
            "power_loss",
            "media_failure",
            "in_process_process_heartbeat",
        ],
        "pilot_exclusion": "do not merge evidence/2026-12135-r18-* pilots",
    }
    (out / "campaign_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    write_header = not summary_path.is_file()
    failures = 0
    infra_failures = 0

    with summary_path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS)
        if write_header:
            w.writeheader()
        for cycle in range(args.start_cycle, args.cycles + 1):
            cycle_path = cycles_dir / f"cycle_{cycle:02d}.json"
            if cycle in done:
                print(f"skip cycle {cycle} (already recorded)", flush=True)
                # Backfill summary once if JSON exists but CSV row missing.
                if cycle not in already_in_summary and cycle_path.is_file():
                    tl = json.loads(cycle_path.read_text(encoding="utf-8"))
                    row = {k: tl.get(k, "") for k in SUMMARY_FIELDS}
                    row["cycle"] = cycle
                    row["ok"] = bool(tl.get("ok"))
                    w.writerow(row)
                    fh.flush()
                    already_in_summary.add(cycle)
                continue

            print(f"=== recovery cycle {cycle}/{args.cycles} ===", flush=True)
            tl = _one_cycle(
                cycle=cycle,
                image=args.image,
                certs=certs,
                host_port=args.host_port,
                name=args.container_name,
                out_cycle=cycle_path,
            )
            if cycle not in already_in_summary:
                row = {k: tl.get(k, "") for k in SUMMARY_FIELDS}
                row["cycle"] = cycle
                row["ok"] = bool(tl.get("ok"))
                w.writerow(row)
                fh.flush()
                already_in_summary.add(cycle)
            done.add(cycle)
            done_path.write_text(
                " ".join(str(x) for x in sorted(done)) + "\n", encoding="utf-8"
            )
            if not tl.get("ok"):
                failures += 1
                if tl.get("failure_class") == "infrastructure":
                    infra_failures += 1
                print(
                    f"FAIL cycle {cycle} class={tl.get('failure_class')}: {tl.get('error')}",
                    flush=True,
                )

    print(
        f"done failures={failures}/{args.cycles} infrastructure={infra_failures}",
        flush=True,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
