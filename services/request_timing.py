"""Request-level latency decomposition for evaluation (opt-in via SAS_TIMING=1).

Two families of fields are emitted:

1. **Exclusive stages** (safe to stack / sum to ``application_total_ms``):
   ``stage_auth_ms``, ``stage_validation_ms``, ``stage_policy_ms``,
   ``stage_database_ms``, ``stage_serialization_ms``, ``stage_other_ms``.

   When a named span encloses DB work (current handlers wrap the service call
   in ``decision``), DB time is subtracted from that span before assigning
   ``stage_policy_ms``. Residual application time becomes ``stage_other_ms``.

2. **Nested diagnostics** (may overlap; do not stack):
   ``nested_decision_ms``, ``nested_database_ms``, ``nested_serialization_ms``.

Operational identity used by the article benches:

    client_ms ≈ application_total_ms + external_ms
    application_total_ms ≈ sum(stage_*)
    application_non_db_ms = application_total_ms − database_ms
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

_ENABLED = os.environ.get("SAS_TIMING", "").strip().lower() in ("1", "true", "yes", "on")

_current: ContextVar["TimingBag | None"] = ContextVar("sas_timing_bag", default=None)
_engine_listeners_installed = False
_install_lock = threading.Lock()


def timing_enabled() -> bool:
    return _ENABLED


def enable_timing(flag: bool = True) -> None:
    """Runtime toggle (tests / local process benches)."""
    global _ENABLED
    _ENABLED = flag


@dataclass
class TimingBag:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    operation: str = ""
    t0_ns: int = field(default_factory=time.perf_counter_ns)
    spans_ns: dict[str, int] = field(default_factory=dict)
    database_ns: int = 0
    _db_open_ns: int | None = None
    tls_reused: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def add_span_ns(self, name: str, delta_ns: int) -> None:
        self.spans_ns[name] = self.spans_ns.get(name, 0) + max(0, delta_ns)

    def note_db_begin(self) -> None:
        self._db_open_ns = time.perf_counter_ns()

    def note_db_end(self) -> None:
        if self._db_open_ns is None:
            return
        self.database_ns += time.perf_counter_ns() - self._db_open_ns
        self._db_open_ns = None

    @contextmanager
    def span(self, name: str) -> Iterator[None]:
        t0 = time.perf_counter_ns()
        try:
            yield
        finally:
            self.add_span_ns(name, time.perf_counter_ns() - t0)

    def application_total_ns(self) -> int:
        return max(0, time.perf_counter_ns() - self.t0_ns)

    def to_dict(self) -> dict[str, Any]:
        app_ns = self.application_total_ns()
        db_ns = self.database_ns
        named = dict(self.spans_ns)

        auth_ns = int(named.get("auth", 0))
        validation_ns = int(named.get("validation", named.get("parse", 0)))
        decision_ns = int(named.get("policy", named.get("decision", 0)))
        serialization_ns = int(named.get("serialization", 0))

        # Exclusive: DB nested inside decision/handler.
        # Auth/validation are NOT exclusive stack fields — FastAPI/Pydantic run
        # before ``_run_op`` (always 0 unless explicitly wrapped). Kept only as
        # optional diagnostics below.
        stage_database = db_ns
        stage_handler = max(0, decision_ns - db_ns)
        stage_serialization = serialization_ns
        accounted = stage_handler + stage_database + stage_serialization
        stage_other = max(0, app_ns - accounted)

        out: dict[str, Any] = {
            "request_id": self.request_id,
            "operation": self.operation,
            "tls_reused": self.tls_reused,
            # Exclusive stages (stackable for article bars)
            "stage_handler_ms": _ns_to_ms(stage_handler),
            "stage_database_ms": _ns_to_ms(stage_database),
            "stage_serialization_ms": _ns_to_ms(stage_serialization),
            "stage_other_ms": _ns_to_ms(stage_other),
            # Aggregates
            "database_ms": _ns_to_ms(db_ns),
            "application_total_ms": _ns_to_ms(app_ns),
            "application_non_db_ms": _ns_to_ms(max(0, app_ns - db_ns)),
            # Nested diagnostics (may overlap — do not stack)
            "nested_decision_ms": _ns_to_ms(decision_ns),
            "nested_database_ms": _ns_to_ms(db_ns),
            "nested_serialization_ms": _ns_to_ms(serialization_ns),
            "optional_auth_ms": _ns_to_ms(auth_ns),
            "optional_validation_ms": _ns_to_ms(validation_ns),
            "stage_semantics": (
                "exclusive={handler,database,serialization,other}; "
                "handler=service body excl. DB; auth/validation optional "
                "(0 unless wrapped — FastAPI validates before handler)"
            ),
        }
        out.update(self.extra)
        return out

    def header_value(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))


def _ns_to_ms(ns: int) -> float:
    return round(ns / 1_000_000.0, 4)


def get_timing() -> TimingBag | None:
    return _current.get()


def start_timing(operation: str = "") -> TimingBag | None:
    if not _ENABLED:
        return None
    bag = TimingBag(operation=operation)
    _current.set(bag)
    return bag


def clear_timing() -> None:
    _current.set(None)


@contextmanager
def timed_span(name: str) -> Iterator[None]:
    bag = get_timing()
    if bag is None:
        yield
        return
    with bag.span(name):
        yield


def install_sqlalchemy_timing(engine: Any) -> None:
    """Accumulate per-request DB cursor time on the active TimingBag."""
    global _engine_listeners_installed
    if not _ENABLED:
        return
    with _install_lock:
        if _engine_listeners_installed:
            return
        from sqlalchemy import event

        @event.listens_for(engine, "before_cursor_execute")
        def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            bag = get_timing()
            if bag is not None:
                bag.note_db_begin()

        @event.listens_for(engine, "after_cursor_execute")
        def _after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            bag = get_timing()
            if bag is not None:
                bag.note_db_end()

        _engine_listeners_installed = True


# ---- Cold-start milestones (process-global, monotonic) ----------------------

_startup_lock = threading.Lock()
_startup: dict[str, Any] = {
    "process_t0_ns": time.perf_counter_ns(),
    "milestones_ns": {},
    "intervals_ns": {},  # name -> duration_ns measured with start/end
    "_open_spans_ns": {},  # name -> start_ns
}


def mark_startup(name: str) -> None:
    """Point milestone (absolute mono time). Later calls overwrite."""
    with _startup_lock:
        _startup["milestones_ns"][name] = time.perf_counter_ns()


def startup_span_begin(name: str) -> None:
    with _startup_lock:
        _startup["_open_spans_ns"][name] = time.perf_counter_ns()
        _startup["milestones_ns"][f"{name}_start"] = _startup["_open_spans_ns"][name]


def startup_span_end(name: str) -> None:
    """Close a span; if the same name ran on RSA and ECDSA, keep the max duration.

    Critical-path policy for dual listeners: both must be ready, so reported
    stage time is max(RSA, ECDSA) rather than last-writer-wins.
    """
    with _startup_lock:
        t1 = time.perf_counter_ns()
        t0 = _startup["_open_spans_ns"].pop(name, None)
        _startup["milestones_ns"][f"{name}_end"] = t1
        if t0 is None:
            return
        dur = max(0, t1 - t0)
        prev = int(_startup["intervals_ns"].get(name, 0))
        _startup["intervals_ns"][name] = max(prev, dur)
        # Keep per-name samples for diagnostics (last few).
        hist = _startup.setdefault("intervals_hist_ns", {})
        hist.setdefault(name, []).append(dur)
        hist[name] = hist[name][-4:]


@contextmanager
def startup_span(name: str) -> Iterator[None]:
    """Measure a cold-start stage with its own start/end (never by cross-stage diff)."""
    startup_span_begin(name)
    try:
        yield
    finally:
        startup_span_end(name)


def startup_snapshot_ms() -> dict[str, Any]:
    """Internal decomposition relative to process entry (monotonic).

    Additive internal stack (for article bars)::

        internal_total ≈ bootstrap + profile + database + tls + internal_other

    ``diag_bind_to_ready_ms`` is nested (server_bind→first_ready) and must NOT
    be stacked with the stages above.

    Dual-listener policy: stage intervals use max() across RSA/ECDSA samples.
    """
    with _startup_lock:
        t0 = int(_startup["process_t0_ns"])
        marks = dict(_startup["milestones_ns"])
        intervals = dict(_startup["intervals_ns"])
        hist = {
            k: [_ns_to_ms(x) for x in v]
            for k, v in dict(_startup.get("intervals_hist_ns") or {}).items()
        }

    rel = {
        k: _ns_to_ms(v - t0)
        for k, v in sorted(marks.items(), key=lambda kv: kv[1])
    }
    intervals_ms = {k: _ns_to_ms(v) for k, v in sorted(intervals.items())}

    chrono: dict[str, float] = {}
    ordered = sorted(marks.items(), key=lambda kv: kv[1])
    for i in range(1, len(ordered)):
        prev_name, prev_ns = ordered[i - 1]
        name, ns = ordered[i]
        delta = ns - prev_ns
        if delta < 0:
            continue
        chrono[f"{prev_name}_to_{name}_ms"] = _ns_to_ms(delta)

    bootstrap = (
        _ns_to_ms(marks.get("imports_done", t0) - t0) if "imports_done" in marks else 0.0
    )
    profile = float(intervals_ms.get("profile_load") or 0.0)
    database = float(intervals_ms.get("database_init") or 0.0)
    # Prefer RSA tls_init; fold ECDSA via max already stored under tls_init_ecc
    tls = max(
        float(intervals_ms.get("tls_init") or 0.0),
        float(intervals_ms.get("tls_init_ecc") or 0.0),
    )
    internal_total = (
        _ns_to_ms(marks["first_ready"] - t0) if "first_ready" in marks else 0.0
    )
    attributed = bootstrap + profile + database + tls
    internal_other = max(0.0, round(internal_total - attributed, 4))

    diag_bind = None
    if (
        "first_ready" in marks
        and "server_bind" in marks
        and marks["first_ready"] >= marks["server_bind"]
    ):
        diag_bind = _ns_to_ms(marks["first_ready"] - marks["server_bind"])

    stages = {
        "python_bootstrap_ms": round(bootstrap, 4),
        "profile_load_ms": round(profile, 4),
        "database_init_ms": round(database, 4),
        "tls_init_ms": round(tls, 4),
        "internal_other_ms": internal_other,
        "internal_total_ms": round(internal_total, 4),
        # Nested diagnostic — do not stack with stages above
        "diag_bind_to_ready_ms": diag_bind,
        "aggregation": "max_across_listeners",
        "observed_via": "shared_process_/ready",
    }
    stages = {k: v for k, v in stages.items() if v is not None}

    return {
        "milestones_ms_from_python_entry": rel,
        "milestones_ms_from_process_start": rel,
        "intervals_ms": intervals_ms,
        "intervals_hist_ms": hist,
        "stages_ms": stages,
        "chronological_segments_ms": chrono,
        "segments_ms": chrono,
        "timing_enabled": _ENABLED,
        "clock": "perf_counter_ns_monotonic",
        "note": (
            "Additive internal: bootstrap+profile+database+tls+internal_other. "
            "diag_bind_to_ready_ms is nested and excluded from that sum. "
            "Dual listeners: interval = max(samples)."
        ),
    }
