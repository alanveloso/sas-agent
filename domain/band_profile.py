"""Immutable band / regulatory profile used by RF decision services."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import log10
from typing import Any, Mapping


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
class EscPolicy:
    """Peer ESC sensor neighborhood protection."""

    low_hz: int
    high_hz: int
    protection_m: float = 40_000.0


@dataclass(frozen=True)
class FssGwblPolicy:
    """FSS coexistence / GWBL-protected sub-band."""

    low_hz: int
    high_hz: int


@dataclass(frozen=True)
class NtiaExclusionPolicy:
    """Default frequency envelope for NTIA / federal exclusion contours."""

    low_hz: int
    high_hz: int


@dataclass(frozen=True)
class DpaPolicy:
    """Scheduled / portal DPA frequency envelope."""

    low_hz: int
    high_hz: int


@dataclass(frozen=True)
class BorderProtectionPolicy:
    """Cross-border PFD protection parameters (e.g. Arrangement R)."""

    low_hz: int
    high_hz: int
    pfd_limit_dbm_m2_mhz: float = -80.0
    rx_height_m: float = 1.5
    itm_freq_mhz: float = 3625.0


def _optional_policy_mapping(
    data: Mapping[str, object], key: str
) -> Mapping[str, object] | None:
    raw = data.get(key)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise TypeError(f"{key} must be a mapping")
    return raw


def _esc_from_mapping(raw: Mapping[str, object] | None) -> EscPolicy | None:
    if raw is None:
        return None
    return EscPolicy(
        low_hz=int(raw["low_hz"]),  # type: ignore[arg-type]
        high_hz=int(raw["high_hz"]),  # type: ignore[arg-type]
        protection_m=float(raw.get("protection_m", 40_000.0)),  # type: ignore[arg-type]
    )


def _fss_gwbl_from_mapping(raw: Mapping[str, object] | None) -> FssGwblPolicy | None:
    if raw is None:
        return None
    return FssGwblPolicy(
        low_hz=int(raw["low_hz"]),  # type: ignore[arg-type]
        high_hz=int(raw["high_hz"]),  # type: ignore[arg-type]
    )


def _ntia_from_mapping(raw: Mapping[str, object] | None) -> NtiaExclusionPolicy | None:
    if raw is None:
        return None
    return NtiaExclusionPolicy(
        low_hz=int(raw["low_hz"]),  # type: ignore[arg-type]
        high_hz=int(raw["high_hz"]),  # type: ignore[arg-type]
    )


def _dpa_from_mapping(raw: Mapping[str, object] | None) -> DpaPolicy | None:
    if raw is None:
        return None
    return DpaPolicy(
        low_hz=int(raw["low_hz"]),  # type: ignore[arg-type]
        high_hz=int(raw["high_hz"]),  # type: ignore[arg-type]
    )


def _border_from_mapping(
    raw: Mapping[str, object] | None,
) -> BorderProtectionPolicy | None:
    if raw is None:
        return None
    return BorderProtectionPolicy(
        low_hz=int(raw["low_hz"]),  # type: ignore[arg-type]
        high_hz=int(raw["high_hz"]),  # type: ignore[arg-type]
        pfd_limit_dbm_m2_mhz=float(raw.get("pfd_limit_dbm_m2_mhz", -80.0)),  # type: ignore[arg-type]
        rx_height_m=float(raw.get("rx_height_m", 1.5)),  # type: ignore[arg-type]
        itm_freq_mhz=float(raw.get("itm_freq_mhz", 3625.0)),  # type: ignore[arg-type]
    )


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
    esc_policy: EscPolicy | None = None
    fss_gwbl_policy: FssGwblPolicy | None = None
    ntia_exclusion_policy: NtiaExclusionPolicy | None = None
    dpa_policy: DpaPolicy | None = None
    border_policy: BorderProtectionPolicy | None = None

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
        if self.fss_gwbl_policy is None and self.protection.fss_gwbl:
            object.__setattr__(
                self,
                "fss_gwbl_policy",
                FssGwblPolicy(
                    low_hz=self.fss_exclusion_low_hz or self.band_low_hz,
                    high_hz=self.band_high_hz,
                ),
            )
        if self.ntia_exclusion_policy is None and self.protection.exclusion_zones:
            object.__setattr__(
                self,
                "ntia_exclusion_policy",
                NtiaExclusionPolicy(
                    low_hz=self.band_low_hz,
                    high_hz=self.band_high_hz,
                ),
            )
        if self.dpa_policy is None and self.protection.dpa:
            object.__setattr__(
                self,
                "dpa_policy",
                DpaPolicy(low_hz=self.band_low_hz, high_hz=self.band_high_hz),
            )
        if self.esc_policy is None and self.protection.esc:
            object.__setattr__(
                self,
                "esc_policy",
                EscPolicy(low_hz=self.band_low_hz, high_hz=self.band_high_hz),
            )
        if self.border_policy is None and self.protection.border_protection:
            mid_mhz = (
                (self.band_low_hz + self.band_high_hz) / 2.0
            ) / 1_000_000.0
            object.__setattr__(
                self,
                "border_policy",
                BorderProtectionPolicy(
                    low_hz=self.band_low_hz,
                    high_hz=self.band_high_hz,
                    itm_freq_mhz=mid_mhz,
                ),
            )
        self._validate_policy_ranges()

    def _validate_policy_ranges(self) -> None:
        for name, policy in (
            ("esc_policy", self.esc_policy),
            ("fss_gwbl_policy", self.fss_gwbl_policy),
            ("ntia_exclusion_policy", self.ntia_exclusion_policy),
            ("dpa_policy", self.dpa_policy),
            ("border_policy", self.border_policy),
        ):
            if policy is None:
                continue
            if policy.high_hz <= policy.low_hz:
                raise ValueError(f"{name}.high_hz must be greater than low_hz")

    @property
    def channel_count(self) -> int:
        """Full-width channels that fit exactly inside the band."""
        return (self.band_high_hz - self.band_low_hz) // self.channel_width_hz

    @property
    def min_without_grant_reports(self) -> int:
        """Number of full-width measurement reports covering the whole band."""
        return self.channel_count

    def gaa_source_id(self) -> str:
        """Stable AuthorizationSource.source_id for the profile's GAA policy."""
        return self.gaa_policy_id or f"GAA_{self.regulatory_rule_id}"

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

    def to_mapping(self) -> dict[str, Any]:
        """Serialize profile fields for diagnostics / config export."""
        def _range(policy: Any) -> dict[str, Any] | None:
            if policy is None:
                return None
            out = {"low_hz": policy.low_hz, "high_hz": policy.high_hz}
            if isinstance(policy, EscPolicy):
                out["protection_m"] = policy.protection_m
            if isinstance(policy, BorderProtectionPolicy):
                out["pfd_limit_dbm_m2_mhz"] = policy.pfd_limit_dbm_m2_mhz
                out["rx_height_m"] = policy.rx_height_m
                out["itm_freq_mhz"] = policy.itm_freq_mhz
            return out

        return {
            "profile_id": self.profile_id,
            "band_low_hz": self.band_low_hz,
            "band_high_hz": self.band_high_hz,
            "channel_width_hz": self.channel_width_hz,
            "regulatory_rule_id": self.regulatory_rule_id,
            "category_a_max_eirp_dbm": self.category_a_max_eirp_dbm,
            "category_b_max_eirp_dbm": self.category_b_max_eirp_dbm,
            "eirp_reference_bandwidth_hz": self.eirp_reference_bandwidth_hz,
            "raster_origin_hz": self.raster_origin_hz,
            "fss_exclusion_low_hz": self.fss_exclusion_low_hz,
            "fss_protection_km": self.fss_protection_km,
            "protection": {
                "pal_gaa": self.protection.pal_gaa,
                "fss_gwbl": self.protection.fss_gwbl,
                "exclusion_zones": self.protection.exclusion_zones,
                "border_protection": self.protection.border_protection,
                "dpa": self.protection.dpa,
                "esc": self.protection.esc,
                "quiet_zones": self.protection.quiet_zones,
            },
            "gaa_policy_id": self.gaa_policy_id,
            "grant_duration_sec": self.grant_duration_sec,
            "heartbeat_interval_sec": self.heartbeat_interval_sec,
            "transmit_expire_sec": self.transmit_expire_sec,
            "esc_policy": _range(self.esc_policy),
            "fss_gwbl_policy": _range(self.fss_gwbl_policy),
            "ntia_exclusion_policy": _range(self.ntia_exclusion_policy),
            "dpa_policy": _range(self.dpa_policy),
            "border_policy": _range(self.border_policy),
        }


CBRS_WINNFORUM_PROFILE_ID = "cbrs_winnforum"
BR_ANATEL_PRIVATIVA_PROFILE_ID = "br_anatel_privativa"
# Legacy AuthorizationSource.source_id used before profile-derived GAA ids.
LEGACY_GAA_SOURCE_ID = "CBRS"


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
        esc_policy=EscPolicy(
            low_hz=3_550_000_000,
            high_hz=3_700_000_000,
            protection_m=40_000.0,
        ),
        fss_gwbl_policy=FssGwblPolicy(low_hz=3_650_000_000, high_hz=3_700_000_000),
        ntia_exclusion_policy=NtiaExclusionPolicy(
            low_hz=3_550_000_000,
            high_hz=3_650_000_000,
        ),
        # Portal DPA MVP historically gated on 3500–3700 MHz.
        dpa_policy=DpaPolicy(low_hz=3_500_000_000, high_hz=3_700_000_000),
        border_policy=BorderProtectionPolicy(
            low_hz=3_650_000_000,
            high_hz=3_700_000_000,
            pfd_limit_dbm_m2_mhz=-80.0,
            rx_height_m=1.5,
            itm_freq_mhz=3625.0,
        ),
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
        fss_gwbl_policy=FssGwblPolicy(low_hz=3_700_000_000, high_hz=3_800_000_000),
        ntia_exclusion_policy=NtiaExclusionPolicy(
            low_hz=3_700_000_000,
            high_hz=3_800_000_000,
        ),
        esc_policy=None,
        dpa_policy=None,
        border_policy=None,
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
        esc_policy=_esc_from_mapping(_optional_policy_mapping(data, "esc_policy")),
        fss_gwbl_policy=_fss_gwbl_from_mapping(
            _optional_policy_mapping(data, "fss_gwbl_policy")
        ),
        ntia_exclusion_policy=_ntia_from_mapping(
            _optional_policy_mapping(data, "ntia_exclusion_policy")
        ),
        dpa_policy=_dpa_from_mapping(_optional_policy_mapping(data, "dpa_policy")),
        border_policy=_border_from_mapping(
            _optional_policy_mapping(data, "border_policy")
        ),
    )
