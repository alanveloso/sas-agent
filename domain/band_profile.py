"""Immutable band / regulatory profile used by RF decision services."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import log10
from typing import Mapping


@dataclass(frozen=True)
class ProtectionFlags:
    """Which incumbent / coexistence policies are active for a band."""

    pal_gaa: bool = True
    fss_gwbl: bool = True
    exclusion_zones: bool = True
    border_protection: bool = True
    dpa: bool = True
    esc: bool = True
    quiet_zones: bool = True


@dataclass(frozen=True)
class BandProfile:
    """Immutable RF / regulatory package for a coordinated band."""

    profile_id: str
    band_low_hz: int
    band_high_hz: int
    channel_width_hz: int
    regulatory_rule_id: str
    category_a_max_eirp_dbm: float = 30.0
    category_b_max_eirp_dbm: float = 47.0
    eirp_reference_bandwidth_hz: int = 10_000_000
    raster_origin_hz: int | None = None
    fss_exclusion_low_hz: int | None = None
    fss_protection_km: float = 150.0
    protection: ProtectionFlags = field(default_factory=ProtectionFlags)
    gaa_policy_id: str | None = None
    grant_duration_sec: int = 900
    heartbeat_interval_sec: int = 60
    transmit_expire_sec: int = 60

    def __post_init__(self) -> None:
        if not self.profile_id or not str(self.profile_id).strip():
            raise ValueError("profile_id must be non-empty")
        if self.band_high_hz <= self.band_low_hz:
            raise ValueError("band_high_hz must be greater than band_low_hz")
        if self.channel_width_hz <= 0:
            raise ValueError("channel_width_hz must be positive")
        if self.eirp_reference_bandwidth_hz <= 0:
            raise ValueError("eirp_reference_bandwidth_hz must be positive")
        if not self.regulatory_rule_id or not str(self.regulatory_rule_id).strip():
            raise ValueError("regulatory_rule_id must be non-empty")
        if self.raster_origin_hz is None:
            object.__setattr__(self, "raster_origin_hz", self.band_low_hz)
        if self.fss_exclusion_low_hz is None:
            object.__setattr__(self, "fss_exclusion_low_hz", self.band_low_hz)
        if self.gaa_policy_id is None:
            object.__setattr__(
                self, "gaa_policy_id", f"{self.regulatory_rule_id}_GAA"
            )
        span = self.band_high_hz - self.band_low_hz
        if span % self.channel_width_hz != 0:
            # Allowed for non-CBRS experiments, but document via derived count.
            pass

    @property
    def channel_count(self) -> int:
        """Full-width channels that fit exactly inside the band."""
        return (self.band_high_hz - self.band_low_hz) // self.channel_width_hz

    @property
    def min_without_grant_reports(self) -> int:
        """Number of full-width measurement reports covering the whole band."""
        return self.channel_count

    def contains_frequency(self, freq_hz: int) -> bool:
        return self.band_low_hz <= freq_hz < self.band_high_hz

    def contains_range(self, low_hz: int, high_hz: int) -> bool:
        return low_hz >= self.band_low_hz and high_hz <= self.band_high_hz

    def eirp_offset_db(self) -> float:
        """Convert dBm/(reference BW) → dBm/MHz."""
        return 10.0 * log10(self.eirp_reference_bandwidth_hz / 1_000_000.0)

    def category_max_eirp_dbm(self, category: str) -> float:
        cat = (category or "A").upper()
        if cat == "A":
            return self.category_a_max_eirp_dbm
        return self.category_b_max_eirp_dbm

    def max_allowed_eirp_mhz(
        self,
        category: str,
        *,
        fcc_max_eirp: float,
        eirp_capability: float | None = None,
    ) -> float:
        """WINNF: request maxEirp is dBm/MHz; category / FCC caps are per reference BW."""
        caps = [
            self.category_max_eirp_dbm(category),
            float(fcc_max_eirp),
        ]
        if eirp_capability is not None:
            caps.append(float(eirp_capability))
        return min(caps) - self.eirp_offset_db()

    def split_channels(self, low: int, high: int) -> list[tuple[int, int]]:
        """
        Split [low, high) into channels aligned to the profile raster.

        Preserves CBRS quirks: leftover head/tail segments shorter than
        channel_width_hz are emitted so edge contain-checks still match.
        """
        channels: list[tuple[int, int]] = []
        width = self.channel_width_hz
        origin = self.raster_origin_hz if self.raster_origin_hz is not None else self.band_low_hz
        start = max(low, self.band_low_hz)
        end = min(high, self.band_high_hz)
        if end <= start:
            return channels

        # Align relative to raster_origin (CBRS: origin == band_low == 3550 MHz).
        # Equivalent to absolute modulo when origin % width == 0.
        offset = (start - origin) % width
        if offset == 0:
            aligned = start
        else:
            aligned = start + (width - offset)

        if start < aligned:
            head_end = min(aligned, end)
            if head_end > start:
                channels.append((start, head_end))

        cur = aligned
        while cur + width <= end:
            channels.append((cur, cur + width))
            cur += width
        if cur < end:
            channels.append((cur, end))
        return channels

    def with_band_limits(self, low_hz: int, high_hz: int) -> BandProfile:
        """Return a copy with overridden band edges (legacy BAND_LOW/HIGH)."""
        return replace(self, band_low_hz=low_hz, band_high_hz=high_hz)


CBRS_WINNFORUM_PROFILE_ID = "cbrs_winnforum"
BR_ANATEL_PRIVATIVA_PROFILE_ID = "br_anatel_privativa"


def cbrs_winnforum_profile() -> BandProfile:
    """Exact behavioural package of the legacy hardcoded CBRS / Part 96 agent."""
    return BandProfile(
        profile_id=CBRS_WINNFORUM_PROFILE_ID,
        band_low_hz=3_550_000_000,
        band_high_hz=3_700_000_000,
        channel_width_hz=10_000_000,
        regulatory_rule_id="FCC_PART_96",
        category_a_max_eirp_dbm=30.0,
        category_b_max_eirp_dbm=47.0,
        eirp_reference_bandwidth_hz=10_000_000,
        raster_origin_hz=3_550_000_000,
        fss_exclusion_low_hz=3_650_000_000,
        fss_protection_km=150.0,
        protection=ProtectionFlags(
            pal_gaa=True,
            fss_gwbl=True,
            exclusion_zones=True,
            border_protection=True,
            dpa=True,
            esc=True,
            quiet_zones=True,
        ),
        gaa_policy_id="FCC_PART_96_GAA",
        grant_duration_sec=900,
        heartbeat_interval_sec=60,
        transmit_expire_sec=60,
    )


def br_anatel_privativa_profile() -> BandProfile:
    """Anatel SLP profile for Brazilian private networks in 3.7–3.8 GHz."""
    return BandProfile(
        profile_id=BR_ANATEL_PRIVATIVA_PROFILE_ID,
        band_low_hz=3_700_000_000,
        band_high_hz=3_800_000_000,
        channel_width_hz=10_000_000,
        regulatory_rule_id="ANATEL_SLP_ACT_374",
        category_a_max_eirp_dbm=47.0,
        category_b_max_eirp_dbm=47.0,
        eirp_reference_bandwidth_hz=100_000_000,
        raster_origin_hz=3_700_000_000,
        fss_exclusion_low_hz=3_700_000_000,
        protection=ProtectionFlags(
            pal_gaa=False,
            fss_gwbl=True,
            exclusion_zones=True,
            border_protection=False,
            dpa=False,
            esc=False,
            quiet_zones=False,
        ),
        gaa_policy_id="ANATEL_SLP_ACT_374_COEXISTENCE",
    )


def profile_from_mapping(data: Mapping[str, object]) -> BandProfile:
    """Build a BandProfile from a plain dict (JSON config / mapping)."""
    protection_raw = data.get("protection") or {}
    if not isinstance(protection_raw, Mapping):
        raise TypeError("protection must be a mapping")
    protection = ProtectionFlags(
        pal_gaa=bool(protection_raw.get("pal_gaa", True)),
        fss_gwbl=bool(protection_raw.get("fss_gwbl", True)),
        exclusion_zones=bool(protection_raw.get("exclusion_zones", True)),
        border_protection=bool(protection_raw.get("border_protection", True)),
        dpa=bool(protection_raw.get("dpa", True)),
        esc=bool(protection_raw.get("esc", True)),
        quiet_zones=bool(protection_raw.get("quiet_zones", True)),
    )
    return BandProfile(
        profile_id=str(data["profile_id"]),
        band_low_hz=int(data["band_low_hz"]),  # type: ignore[arg-type]
        band_high_hz=int(data["band_high_hz"]),  # type: ignore[arg-type]
        channel_width_hz=int(data["channel_width_hz"]),  # type: ignore[arg-type]
        regulatory_rule_id=str(data["regulatory_rule_id"]),
        category_a_max_eirp_dbm=float(data.get("category_a_max_eirp_dbm", 30.0)),  # type: ignore[arg-type]
        category_b_max_eirp_dbm=float(data.get("category_b_max_eirp_dbm", 47.0)),  # type: ignore[arg-type]
        eirp_reference_bandwidth_hz=int(
            data.get("eirp_reference_bandwidth_hz", 10_000_000)  # type: ignore[arg-type]
        ),
        raster_origin_hz=(
            int(data["raster_origin_hz"])  # type: ignore[arg-type]
            if data.get("raster_origin_hz") is not None
            else None
        ),
        fss_exclusion_low_hz=(
            int(data["fss_exclusion_low_hz"])  # type: ignore[arg-type]
            if data.get("fss_exclusion_low_hz") is not None
            else None
        ),
        fss_protection_km=float(data.get("fss_protection_km", 150.0)),  # type: ignore[arg-type]
        protection=protection,
        gaa_policy_id=(
            str(data["gaa_policy_id"])
            if data.get("gaa_policy_id") is not None
            else None
        ),
        grant_duration_sec=int(data.get("grant_duration_sec", 900)),  # type: ignore[arg-type]
        heartbeat_interval_sec=int(data.get("heartbeat_interval_sec", 60)),  # type: ignore[arg-type]
        transmit_expire_sec=int(data.get("transmit_expire_sec", 60)),  # type: ignore[arg-type]
    )
