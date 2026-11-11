"""Shared helpers for comparative load benchmarks (cold start + mTLS stress).

Provides mTLS SSL context construction, long-format CSV I/O, reproducibility
manifests, percentiles, Docker lifecycle wrappers and HTTP readiness polling.
"""

from __future__ import annotations

import csv
import hashlib
import json
import platform
import ssl
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from math import ceil
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import httpx

LONG_FIELDS: tuple[str, ...] = ("architecture", "load_level", "metric", "value")

DEFAULT_SEED = 42
DEFAULT_N = 30
DEFAULT_WARMUP = 3
BATCH_MAX = 100
POLL_INTERVAL_SEC = 0.05

RSA_CIPHERS = (
    "AES128-GCM-SHA256:"
    "AES256-GCM-SHA384:"
    "ECDHE-RSA-AES128-GCM-SHA256"
)
ECDSA_CIPHERS = (
    "ECDHE-ECDSA-AES128-GCM-SHA256:"
    "ECDHE-ECDSA-AES256-GCM-SHA384"
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def build_mtls_context(certs_dir: Path, ciphers: str = RSA_CIPHERS) -> ssl.SSLContext:
    """Build a TLS 1.2 client context matching the WInnForum harness handshake."""
    certs_dir = certs_dir.expanduser().resolve()
    ca = certs_dir / "ca.cert"
    cert = certs_dir / "domain_proxy.cert"
    key = certs_dir / "domain_proxy.key"
    missing = [p.name for p in (ca, cert, key) if not p.exists()]
    if missing:
        raise FileNotFoundError(
            f"Missing mTLS client files in {certs_dir}: {missing}"
        )

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=str(ca))
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    ctx.set_ciphers(ciphers)
    return ctx


def cert_sha256_fingerprint(path: Path) -> str:
    """SHA-256 of the first certificate in a PEM file, as ``AA:BB:...``."""
    pem = path.read_text(encoding="ascii")
    end = "-----END CERTIFICATE-----"
    der = ssl.PEM_cert_to_DER_cert(pem[: pem.index(end) + len(end)])
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile; ``p`` in [0, 100]."""
    if not values:
        return None
    if p < 0 or p > 100:
        raise ValueError(f"percentile p must be in [0, 100], got {p}")
    ordered = sorted(values)
    if p == 0:
        return float(ordered[0])
    rank = max(1, ceil(p / 100.0 * len(ordered)))
    return float(ordered[rank - 1])


def write_long_csv(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    """Write long-format metric rows: architecture, load_level, metric, value."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(LONG_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in LONG_FIELDS})


def append_long_row(
    writer: csv.DictWriter,
    fh: Any,
    architecture: str,
    load_level: int | str,
    metric: str,
    value: float | int | str,
) -> dict[str, Any]:
    row = {
        "architecture": architecture,
        "load_level": load_level,
        "metric": metric,
        "value": value,
    }
    writer.writerow(row)
    fh.flush()
    return row


def package_versions(names: Sequence[str]) -> dict[str, str | None]:
    pkgs: dict[str, str | None] = {}
    for name in names:
        try:
            pkgs[name] = importlib_metadata.version(name)
        except importlib_metadata.PackageNotFoundError:
            pkgs[name] = None
    return pkgs


def git_snapshot(repo_dir: Path | None = None) -> dict[str, Any]:
    """Commit hash + dirty state of the repository under evaluation."""
    cwd = str(repo_dir) if repo_dir else str(Path(__file__).resolve().parents[2])
    info: dict[str, Any] = {}
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=cwd,
        )
        info["commit"] = head.stdout.strip() if head.returncode == 0 else None
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=cwd,
        )
        if status.returncode == 0:
            dirty_files = [ln for ln in status.stdout.splitlines() if ln.strip()]
            info["dirty"] = bool(dirty_files)
            info["dirty_file_count"] = len(dirty_files)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        info["error"] = str(exc)
    return info


def host_load_snapshot() -> dict[str, Any]:
    """Load average, prior CPU usage and temperatures right before a run.

    Primary thermal sensor for gates is ``k10temp:Tctl`` when present; other
    sensors are recorded for diagnostics but are not the gate metric.
    """
    info: dict[str, Any] = {}
    try:
        import os as _os

        la1, la5, la15 = _os.getloadavg()
        info["loadavg_1m"] = la1
        info["loadavg_5m"] = la5
        info["loadavg_15m"] = la15
    except OSError as exc:
        info["loadavg_error"] = str(exc)
    try:
        import psutil

        info["cpu_percent_prior"] = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        info["memory_used_percent_prior"] = mem.percent
        temps = getattr(psutil, "sensors_temperatures", lambda: {})() or {}
        readings: dict[str, float] = {}
        for sensor, entries in temps.items():
            for i, entry in enumerate(entries):
                if entry.current is not None:
                    label = entry.label or f"{sensor}_{i}"
                    readings[f"{sensor}:{label}"] = float(entry.current)
        if readings:
            info["temperatures_c"] = readings
            info["temperature_max_c"] = max(readings.values())
            tctl = readings.get("k10temp:Tctl")
            if tctl is None:
                # fallback: any k10temp reading labeled Tctl / Tdie
                for k, v in readings.items():
                    if k.startswith("k10temp:") and ("Tctl" in k or k.endswith(":Tctl")):
                        tctl = v
                        break
            if tctl is not None:
                info["temperature_tctl_c"] = float(tctl)
    except ImportError:
        info["psutil_error"] = "psutil not installed"
    except Exception as exc:  # noqa: BLE001 — sensors are best-effort
        info["sensors_error"] = str(exc)
    return info


def cpu_freq_snapshot() -> dict[str, Any]:
    """Current CPU frequency policy / readings (best-effort)."""
    info: dict[str, Any] = {}
    try:
        import psutil

        freq = psutil.cpu_freq(percpu=True)
        if freq:
            currents = [f.current for f in freq if f and f.current]
            if currents:
                info["freq_mhz_mean"] = float(sum(currents) / len(currents))
                info["freq_mhz_min"] = float(min(currents))
                info["freq_mhz_max"] = float(max(currents))
                info["freq_mhz_per_cpu"] = [float(f.current) for f in freq if f]
        gov_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
        if gov_path.is_file():
            info["governor"] = gov_path.read_text(encoding="utf-8").strip()
    except Exception as exc:  # noqa: BLE001
        info["error"] = str(exc)
    return info


def thermal_throttling_hints() -> dict[str, Any]:
    """Best-effort hints of thermal pressure (RAPL / sysfs / journal)."""
    hints: dict[str, Any] = {"sources": []}
    # AMD/Intel powercap energy counters presence only (not a throttle flag).
    powercap = Path("/sys/class/powercap")
    if powercap.is_dir():
        hints["powercap_zones"] = sorted(p.name for p in powercap.iterdir())[:12]
        hints["sources"].append("powercap")
    # Some kernels expose package throttle counts under thermal zones.
    thermal = Path("/sys/class/thermal")
    if thermal.is_dir():
        trips = []
        for zone in sorted(thermal.glob("thermal_zone*"))[:8]:
            typ = (zone / "type").read_text(encoding="utf-8").strip() if (zone / "type").is_file() else ""
            temp_f = zone / "temp"
            try:
                milli = int(temp_f.read_text(encoding="utf-8").strip()) if temp_f.is_file() else None
            except ValueError:
                milli = None
            trips.append({"zone": zone.name, "type": typ, "temp_c": milli / 1000.0 if milli else None})
        hints["thermal_zones"] = trips
        hints["sources"].append("sysfs_thermal")
    return hints


def wait_thermal_gate(
    *,
    max_tctl_c: float = 65.0,
    max_load1: float = 1.0,
    max_cpu_pct: float = 10.0,
    poll_sec: float = 5.0,
    timeout_sec: float = 600.0,
    out_path: Path | None = None,
    label: str = "thermal_gate",
) -> dict[str, Any]:
    """Block until host is cool/idle enough, or timeout; always write a snapshot.

    Gate metric is ``k10temp:Tctl`` (not max-of-all-sensors). Falls back to
    ``temperature_max_c`` only if Tctl is unavailable.
    """
    import time as _time

    t0 = _time.perf_counter()
    samples: list[dict[str, Any]] = []
    passed = False
    while True:
        snap = host_load_snapshot()
        snap["cpu_freq"] = cpu_freq_snapshot()
        snap["throttling_hints"] = thermal_throttling_hints()
        snap["elapsed_wait_sec"] = round(_time.perf_counter() - t0, 2)
        samples.append(snap)
        temp = snap.get("temperature_tctl_c")
        if temp is None:
            temp = snap.get("temperature_max_c")
        load1 = snap.get("loadavg_1m")
        cpu = snap.get("cpu_percent_prior")
        temp_ok = temp is None or float(temp) <= max_tctl_c
        load_ok = load1 is None or float(load1) <= max_load1
        cpu_ok = cpu is None or float(cpu) <= max_cpu_pct
        if temp_ok and load_ok and cpu_ok:
            passed = True
            break
        if _time.perf_counter() - t0 >= timeout_sec:
            break
        _time.sleep(poll_sec)

    record: dict[str, Any] = {
        "label": label,
        "timestamp_utc": utc_now_iso(),
        "passed": passed,
        "timed_out": not passed,
        "sensor": "k10temp:Tctl",
        "max_tctl_c_threshold": max_tctl_c,
        "max_load1_threshold": max_load1,
        "max_cpu_pct_threshold": max_cpu_pct,
        "wait_sec": round(_time.perf_counter() - t0, 2),
        "final": samples[-1] if samples else {},
        "n_samples": len(samples),
        "git": git_snapshot(),
    }
    if out_path is not None:
        write_manifest(out_path, record)
    return record


def docker_version() -> str | None:
    try:
        proc = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            return proc.stdout.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return None


def image_digest(image: str) -> str | None:
    try:
        proc = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}",
                image,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            return None
        digests = json.loads(proc.stdout.strip() or "[]")
        return digests[0] if digests else None
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def capture_manifest(
    *,
    seed: int,
    n: int,
    warmup: int,
    script_paths: Sequence[Path],
    extra: Mapping[str, Any] | None = None,
    certs_dir: Path | None = None,
) -> dict[str, Any]:
    """Environment snapshot aligned with ``benchmarks/run_all.py``."""
    info: dict[str, Any] = {
        "timestamp_utc": utc_now_iso(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "machine": platform.machine(),
        "node": platform.node(),
        "seed": seed,
        "n": n,
        "warmup": warmup,
        "packages": package_versions(
            ("httpx", "psutil", "fastapi", "uvicorn", "sqlalchemy")
        ),
        "docker_version": docker_version(),
        "git": git_snapshot(),
        "host_load": host_load_snapshot(),
        "script_hashes": {
            p.name: sha256_file(p) for p in script_paths if p.is_file()
        },
    }

    try:
        import os as _os

        proc = subprocess.run(
            ["lscpu"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env={**_os.environ, "LC_ALL": "C"},
        )
        if proc.returncode == 0:
            info["lscpu"] = proc.stdout
            for line in proc.stdout.splitlines():
                if "Model name" in line:
                    info["cpu_model"] = line.split(":", 1)[-1].strip()
                if line.strip().startswith("CPU(s):") or (
                    line.split(":")[0].strip() == "CPU(s)"
                ):
                    if "cpu_count" not in info:
                        info["cpu_count"] = line.split(":", 1)[-1].strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        info["lscpu_error"] = str(exc)

    try:
        mem = Path("/proc/meminfo").read_text(encoding="utf-8")
        info["meminfo_total"] = next(
            (ln for ln in mem.splitlines() if ln.startswith("MemTotal:")),
            "",
        )
    except OSError as exc:
        info["meminfo_error"] = str(exc)

    if certs_dir is not None:
        ca = certs_dir.expanduser().resolve() / "ca.cert"
        info["certs_dir"] = str(certs_dir.expanduser().resolve())
        info["certs_fingerprint"] = sha256_file(ca) if ca.is_file() else None

    if extra:
        info.update(dict(extra))
    return info


def write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


@dataclass(frozen=True)
class DockerRunSpec:
    image: str
    name: str
    host_port: int
    certs_dir: Path
    database_path: str = "/data/sas_mvp.db"
    band_profile: str = "cbrs_winnforum"
    container_port: int = 9000
    # Optional host path to a BandProfile JSON, mounted read-only and exported
    # as BAND_PROFILE_FILE (takes precedence over band_profile inside the app).
    band_profile_file: Path | None = None
    # Optional slice overrides (Hz) exported as BAND_LOW / BAND_HIGH.
    band_low_hz: int | None = None
    band_high_hz: int | None = None
    # Extra container environment variables (e.g. SAS_TLS_CLIENT_AUTH=none).
    extra_env: Mapping[str, str] | None = None


def docker_rm(name: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", name],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def docker_run(spec: DockerRunSpec) -> str:
    """Start a detached container; returns container id. Timer starts before call."""
    docker_rm(spec.name)
    certs = str(spec.certs_dir.expanduser().resolve())
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        spec.name,
        "-p",
        f"{spec.host_port}:{spec.container_port}",
        "-v",
        f"{certs}:/app/src/harness/certs:ro",
        "-e",
        f"SAS_DATABASE_PATH={spec.database_path}",
        "-e",
        f"BAND_PROFILE={spec.band_profile}",
    ]
    if spec.band_profile_file is not None:
        profile_host = str(spec.band_profile_file.expanduser().resolve())
        cmd += [
            "-v",
            f"{profile_host}:/config/band_profile.json:ro",
            "-e",
            "BAND_PROFILE_FILE=/config/band_profile.json",
        ]
    if spec.band_low_hz is not None:
        cmd += ["-e", f"BAND_LOW={spec.band_low_hz}"]
    if spec.band_high_hz is not None:
        cmd += ["-e", f"BAND_HIGH={spec.band_high_hz}"]
    extra_env = dict(spec.extra_env or {})
    admin_cert = spec.certs_dir.expanduser().resolve() / "domain_proxy.cert"
    if "SAS_ADMIN_CERT_SHA256" not in extra_env and admin_cert.is_file():
        # /admin is fail-closed; authorize the client cert the benches use.
        extra_env["SAS_ADMIN_CERT_SHA256"] = cert_sha256_fingerprint(admin_cert)
    for key, value in extra_env.items():
        cmd += ["-e", f"{key}={value}"]
    cmd.append(spec.image)
    proc = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker run failed for {spec.image}: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def wait_http_200(
    url: str,
    ctx: ssl.SSLContext,
    timeout_sec: float,
    *,
    poll_interval_sec: float = POLL_INTERVAL_SEC,
    trace: dict[str, Any] | None = None,
) -> tuple[bool, int, str | None]:
    """Poll ``url`` until HTTP 200 or timeout.

    Each attempt is a blocking GET (2 s timeout) followed, on failure, by a
    ``poll_interval_sec`` sleep, so the effective period is attempt + sleep.

    Returns ``(ok, poll_count, error)``. If ``trace`` is given it receives
    ``perf_counter`` instants ``last_failed_start``, ``success_start`` and
    ``success_end`` (None when absent), ``last_failed_error`` and the JSON
    ``body`` of the first 200.
    """
    if trace is not None:
        trace.update(
            poll_interval_sec=poll_interval_sec,
            last_failed_start=None,
            last_failed_error=None,
            success_start=None,
            success_end=None,
            body=None,
        )
    deadline = time.perf_counter() + timeout_sec
    polls = 0
    last_error: str | None = None
    with httpx.Client(verify=ctx, timeout=2.0) as client:
        while time.perf_counter() < deadline:
            polls += 1
            t_attempt = time.perf_counter()
            try:
                resp = client.get(url)
                if resp.status_code == 200:
                    if trace is not None:
                        trace["success_start"] = t_attempt
                        trace["success_end"] = time.perf_counter()
                        try:
                            trace["body"] = resp.json()
                        except ValueError:
                            pass
                    return True, polls, None
                last_error = f"HTTP {resp.status_code}"
            except Exception as exc:  # noqa: BLE001 — poll until ready
                last_error = str(exc)
            if trace is not None:
                trace["last_failed_start"] = t_attempt
                trace["last_failed_error"] = last_error
            time.sleep(poll_interval_sec)
    return False, polls, last_error or "timeout"


def chunked(items: Sequence[Any], size: int) -> list[list[Any]]:
    if size < 1:
        raise ValueError("chunk size must be >= 1")
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def parse_docker_stats_line(stdout: str) -> tuple[float | None, float | None]:
    """Parse ``CPUPerc,MemUsage`` from ``docker stats --no-stream`` format.

    MemUsage looks like ``12.3MiB / 7.7GiB`` or ``1.2GiB / ...``.
    """
    line = stdout.strip().splitlines()[-1] if stdout.strip() else ""
    if not line or "," not in line:
        return None, None
    cpu_s, mem_s = line.split(",", 1)
    cpu_s = cpu_s.strip().rstrip("%")
    mem_part = mem_s.strip().split("/", 1)[0].strip()
    try:
        cpu = float(cpu_s)
    except ValueError:
        cpu = None
    rss_mb = _parse_mem_to_mb(mem_part)
    return cpu, rss_mb


def _parse_mem_to_mb(token: str) -> float | None:
    token = token.strip()
    units = {
        "B": 1 / (1024 * 1024),
        "KiB": 1 / 1024,
        "KB": 1 / 1000,
        "MiB": 1.0,
        "MB": 1.0,
        "GiB": 1024.0,
        "GB": 1000.0,
    }
    for suffix, factor in sorted(units.items(), key=lambda x: -len(x[0])):
        if token.endswith(suffix):
            try:
                return float(token[: -len(suffix)].strip()) * factor
            except ValueError:
                return None
    try:
        return float(token)
    except ValueError:
        return None


def docker_stats_sample(container: str) -> tuple[float | None, float | None]:
    proc = subprocess.run(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{.CPUPerc}},{{.MemUsage}}",
            container,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        return None, None
    return parse_docker_stats_line(proc.stdout)
