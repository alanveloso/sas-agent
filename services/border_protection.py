"""Profile-driven, provider-injectable border PFD protection."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from domain.band_profile import BandProfile, BorderProtectionPolicy
from services.profile_manager import resolve_optional

# Optional harness reference models (ITM, border geometry, antenna gains).
_HARNESS = Path(__file__).resolve().parents[2] / "src" / "harness"
if _HARNESS.is_dir() and str(_HARNESS) not in sys.path:
    sys.path.insert(0, str(_HARNESS))


def _overlaps_border_band(
    low_hz: int, high_hz: int, policy: BorderProtectionPolicy
) -> bool:
    # Same gate style as BPR harness: highFrequency > policy.low_hz and overlaps.
    return high_hz > policy.low_hz and low_hz < policy.high_hz


@dataclass(frozen=True)
class BorderPoint:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class PropagationResult:
    loss_db: float
    bearing_deg: float


class BorderGeoProvider(Protocol):
    """External border geometry, propagation and antenna calculations."""

    def closest_border_point(
        self,
        latitude: float,
        longitude: float,
        antenna_azimuth: Any,
        antenna_beamwidth: Any,
    ) -> BorderPoint | None: ...

    def propagation(
        self,
        latitude: float,
        longitude: float,
        height_m: float,
        border: BorderPoint,
        policy: BorderProtectionPolicy,
        *,
        indoor: bool,
        height_is_amsl: bool,
    ) -> PropagationResult: ...

    def antenna_gain(
        self,
        bearing_deg: float,
        antenna_azimuth: Any,
        antenna_beamwidth: Any,
        maximum_gain_db: float,
    ) -> float: ...


class HarnessBorderGeoProvider:
    """Adapter for the optional WInnForum reference-model package."""

    def __init__(self) -> None:
        from reference_models.antenna import antenna
        from reference_models.geo import utils
        from reference_models.propagation import wf_itm

        self._antenna = antenna
        self._utils = utils
        self._propagation = wf_itm

    def closest_border_point(
        self,
        latitude: float,
        longitude: float,
        antenna_azimuth: Any,
        antenna_beamwidth: Any,
    ) -> BorderPoint | None:
        in_zone, border_lat, border_lon = (
            self._utils.CheckCbsdInBorderSharingZone(
                latitude,
                longitude,
                antenna_azimuth,
                antenna_beamwidth,
            )
        )
        if not in_zone or border_lat is None or border_lon is None:
            return None
        return BorderPoint(float(border_lat), float(border_lon))

    def propagation(
        self,
        latitude: float,
        longitude: float,
        height_m: float,
        border: BorderPoint,
        policy: BorderProtectionPolicy,
        *,
        indoor: bool,
        height_is_amsl: bool,
    ) -> PropagationResult:
        result = self._propagation.CalcItmPropagationLoss(
            latitude,
            longitude,
            height_m,
            border.latitude,
            border.longitude,
            policy.rx_height_m,
            reliability=0.5,
            cbsd_indoor=indoor,
            freq_mhz=policy.itm_freq_mhz,
            is_height_cbsd_amsl=height_is_amsl,
        )
        return PropagationResult(
            loss_db=float(result.db_loss),
            bearing_deg=float(result.incidence_angles.hor_cbsd),
        )

    def antenna_gain(
        self,
        bearing_deg: float,
        antenna_azimuth: Any,
        antenna_beamwidth: Any,
        maximum_gain_db: float,
    ) -> float:
        return float(
            self._antenna.GetStandardAntennaGains(
                bearing_deg,
                antenna_azimuth,
                antenna_beamwidth,
                maximum_gain_db,
            )
        )


def _default_provider() -> BorderGeoProvider | None:
    try:
        return HarnessBorderGeoProvider()
    except ImportError:
        return None


def violates_border_pfd(
    installation: dict[str, Any],
    max_eirp: float,
    low_hz: int,
    high_hz: int,
    *,
    profile: BandProfile | None = None,
    provider: BorderGeoProvider | None = None,
) -> bool:
    """Return whether a profile border-PFD policy rejects the operation."""
    active = resolve_optional(profile)
    if not active.protection.border_protection or active.border_policy is None:
        return False
    policy = active.border_policy
    if not _overlaps_border_band(low_hz, high_hz, policy):
        return False

    try:
        lat = float(installation["latitude"])
        lon = float(installation["longitude"])
    except (KeyError, TypeError, ValueError):
        return False

    ant_azi = installation.get("antennaAzimuth")
    ant_bw = installation.get("antennaBeamwidth")
    try:
        max_ant_gain = float(installation.get("antennaGain") or 0)
    except (TypeError, ValueError):
        max_ant_gain = 0.0

    geo = provider or _default_provider()
    if geo is None:
        return False

    try:
        border = geo.closest_border_point(
            lat,
            lon,
            ant_azi,
            ant_bw,
        )
    except Exception:
        return False
    if border is None:
        return False

    height = float(installation.get("height") or 0)
    height_type = installation.get("heightType") or "AGL"
    indoor = bool(installation.get("indoorDeployment"))

    try:
        propagation = geo.propagation(
            lat,
            lon,
            height,
            border,
            policy,
            indoor=indoor,
            height_is_amsl=height_type == "AMSL",
        )
        ant_gain = geo.antenna_gain(
            propagation.bearing_deg,
            ant_azi,
            ant_bw,
            max_ant_gain,
        )
    except Exception:
        # Fail closed once the device is known to be in the sharing zone.
        return True

    pfd = max_eirp - max_ant_gain + ant_gain - propagation.loss_db + 32.6
    return pfd > policy.pfd_limit_dbm_m2_mhz


def violates_canadian_border_pfd(
    installation: dict[str, Any],
    max_eirp: float,
    low_hz: int,
    high_hz: int,
    *,
    profile: BandProfile | None = None,
    provider: BorderGeoProvider | None = None,
) -> bool:
    """Backward-compatible alias for the former jurisdiction-specific API."""
    return violates_border_pfd(
        installation,
        max_eirp,
        low_hz,
        high_hz,
        profile=profile,
        provider=provider,
    )
