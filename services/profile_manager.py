"""Active BandProfile registry and environment-driven selection."""

from __future__ import annotations

import os
import threading
from typing import Callable

from domain.band_profile import (
    CBRS_WINNFORUM_PROFILE_ID,
    BandProfile,
    cbrs_winnforum_profile,
)

_DEFAULT_PROFILE_ENV = "BAND_PROFILE"
_lock = threading.RLock()
_registry: dict[str, Callable[[], BandProfile]] = {
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
) -> BandProfile:
    """
    Resolve a profile by id or BAND_PROFILE env (default: cbrs_winnforum).

    Raises KeyError for unknown ids and ValueError for invalid band overrides.
    """
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
) -> BandProfile:
    """Load and cache the process-wide active profile (idempotent)."""
    global _active, _initialized
    with _lock:
        if _initialized and not force and _active is not None:
            return _active
        profile = resolve_profile(profile_id)
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
