"""Canadian border PFD protection for Grant.

Uses the active BandProfile.border_policy (Arrangement R parameters for CBRS).
CBSDs in the Border Sharing Zone whose requested EIRP would produce PFD above
the profile limit at the closest border point must be rejected with responseCode 400.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from domain.band_profile import BandProfile, BorderProtectionPolicy
from services.profile_manager import resolve_optional

# Harness reference models (ITM, Canadian border geometry, antenna gains).
_HARNESS = Path(__file__).resolve().parents[2] / "src" / "harness"
if _HARNESS.is_dir() and str(_HARNESS) not in sys.path:
    sys.path.insert(0, str(_HARNESS))


def _overlaps_border_band(
    low_hz: int, high_hz: int, policy: BorderProtectionPolicy
) -> bool:
    # Same gate style as BPR harness: highFrequency > policy.low_hz and overlaps.
    return high_hz > policy.low_hz and low_hz < policy.high_hz


def violates_canadian_border_pfd(
    installation: dict[str, Any],
    max_eirp: float,
    low_hz: int,
    high_hz: int,
    *,
    profile: BandProfile | None = None,
) -> bool:
    """Return True if the grant must be rejected (responseCode 400)."""
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

    try:
        from reference_models.antenna import antenna
        from reference_models.geo import utils
        from reference_models.propagation import wf_itm
    except ImportError:
        return False

    try:
        in_zone, border_lat, border_lon = utils.CheckCbsdInBorderSharingZone(
            lat, lon, ant_azi, ant_bw
        )
    except Exception:
        return False
    if not in_zone or border_lat is None or border_lon is None:
        return False

    height = float(installation.get("height") or 0)
    height_type = installation.get("heightType") or "AGL"
    indoor = bool(installation.get("indoorDeployment"))

    try:
        propagation = wf_itm.CalcItmPropagationLoss(
            lat,
            lon,
            height,
            border_lat,
            border_lon,
            policy.rx_height_m,
            reliability=0.5,
            cbsd_indoor=indoor,
            freq_mhz=policy.itm_freq_mhz,
            is_height_cbsd_amsl=(height_type == "AMSL"),
        )
        pl = propagation.db_loss
        bearing = propagation.incidence_angles.hor_cbsd
        ant_gain = antenna.GetStandardAntennaGains(
            bearing, ant_azi, ant_bw, max_ant_gain
        )
    except Exception:
        # Missing terrain / ITM failure while inside sharing zone: reject to
        # satisfy Arrangement R rather than incorrectly authorizing.
        return True

    # PFD = requested_eirp - maxAntGain + effectiveGain - PL + 32.6
    pfd = max_eirp - max_ant_gain + ant_gain - pl + 32.6
    return pfd > policy.pfd_limit_dbm_m2_mhz
