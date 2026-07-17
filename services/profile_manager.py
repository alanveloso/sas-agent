"""Active BandProfile registry and environment-driven selection."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Callable

from domain.band_profile import (
    BR_ANATEL_PRIVATIVA_PROFILE_ID,
    CBRS_WINNFORUM_PROFILE_ID,
    BandProfile,
    br_anatel_privativa_profile,
    cbrs_winnforum_profile,
    profile_from_mapping,
)

_DEFAULT_PROFILE_ENV = "BAND_PROFILE"
_PROFILE_FILE_ENV = "BAND_PROFILE_FILE"
_lock = threading.RLock()
_registry: dict[str, Callable[[], BandProfile]] = {
    BR_ANATEL_PRIVATIVA_PROFILE_ID: br_anatel_privativa_profile,
    CBRS_WINNFORUM_PROFILE_ID: cbrs_winnforum_profile,
}
_active: BandProfile | None = None
_initialized = False


def register_profile(profile_id: str, factory: Callable[[], BandProfile]) -> None:
    """Register (or replace) a named profile factory."""
    with _lock:
        _registry[profile_id] = factory


def list_profile_ids() -> list[str]:
    with _lock:
        return sorted(_registry.keys())


def get_profile(profile_id: str) -> BandProfile:
    with _lock:
        factory = _registry.get(profile_id)
        if factory is None:
            known = ", ".join(sorted(_registry)) or "(none)"
            raise KeyError(f"Unknown band profile {profile_id!r}; known: {known}")
        return factory()


def load_profile_from_file(path: str | Path) -> BandProfile:
    """
    Load a BandProfile from a JSON configuration file and register it.

    Required keys: profile_id, band_low_hz, band_high_hz, channel_width_hz,
    regulatory_rule_id. Optional keys match BandProfile / protection flags.
    """
    file_path = Path(path).expanduser().resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"Band profile file not found: {file_path}")
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in band profile file {file_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise TypeError(
            f"Band profile file {file_path} must contain a JSON object, "
            f"got {type(raw).__name__}"
        )
    profile = profile_from_mapping(raw)

    def factory(p: BandProfile = profile) -> BandProfile:
        return p

    register_profile(profile.profile_id, factory)
    return profile


def _band_limit_override(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return None
    return int(raw)


def _apply_legacy_band_env(profile: BandProfile) -> BandProfile:
    """Honor BAND_LOW / BAND_HIGH overrides used by existing deployments."""
    low = _band_limit_override("BAND_LOW")
    high = _band_limit_override("BAND_HIGH")
    if low is None and high is None:
        return profile
    new_low = low if low is not None else profile.band_low_hz
    new_high = high if high is not None else profile.band_high_hz
    if new_low == profile.band_low_hz and new_high == profile.band_high_hz:
        return profile
    return profile.with_band_limits(new_low, new_high)


def resolve_profile(
    profile_id: str | None = None,
    *,
    apply_band_env: bool = True,
    profile_file: str | Path | None = None,
) -> BandProfile:
    """
    Resolve a profile from BAND_PROFILE_FILE, an explicit id, or BAND_PROFILE.

    Precedence:
    1. ``profile_file`` argument or ``BAND_PROFILE_FILE`` env (JSON config);
    2. ``profile_id`` argument or ``BAND_PROFILE`` env;
    3. built-in default ``cbrs_winnforum``.

    Raises KeyError for unknown ids, FileNotFoundError / ValueError for bad files,
    and ValueError for invalid band overrides.
    """
    file_path = profile_file
    if file_path is None:
        file_env = (os.environ.get(_PROFILE_FILE_ENV) or "").strip()
        file_path = file_env or None

    if file_path:
        profile = load_profile_from_file(file_path)
    else:
        chosen = (profile_id or os.environ.get(_DEFAULT_PROFILE_ENV) or "").strip()
        if not chosen:
            chosen = CBRS_WINNFORUM_PROFILE_ID
        profile = get_profile(chosen)

    if apply_band_env:
        profile = _apply_legacy_band_env(profile)
    return profile


def initialize_active_profile(
    profile_id: str | None = None,
    *,
    force: bool = False,
    profile_file: str | Path | None = None,
) -> BandProfile:
    """Load and cache the process-wide active profile (idempotent)."""
    global _active, _initialized
    with _lock:
        if _initialized and not force and _active is not None:
            return _active
        profile = resolve_profile(profile_id, profile_file=profile_file)
        _active = profile
        _initialized = True
        return profile


def get_active_band_profile() -> BandProfile:
    """
    Return the active profile, initializing from the environment if needed.

    Safe to call from FastAPI Depends() and from service fallbacks.
    """
    global _active, _initialized
    with _lock:
        if _active is not None:
            return _active
        return initialize_active_profile()


def set_active_band_profile(profile: BandProfile) -> BandProfile:
    """Explicitly set the active profile (tests / custom bootstrap)."""
    global _active, _initialized
    with _lock:
        _active = profile
        _initialized = True
        return _active


def reset_active_band_profile() -> None:
    """Clear the cached active profile (tests)."""
    global _active, _initialized
    with _lock:
        _active = None
        _initialized = False


def resolve_optional(profile: BandProfile | None) -> BandProfile:
    """Service-level helper: None → active / default CBRS profile."""
    if profile is not None:
        return profile
    return get_active_band_profile()
