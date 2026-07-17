"""Domain models shared across SAS agent services."""

from domain.band_profile import (
    BR_ANATEL_PRIVATIVA_PROFILE_ID,
    CBRS_WINNFORUM_PROFILE_ID,
    BandProfile,
    ProtectionFlags,
    br_anatel_privativa_profile,
    cbrs_winnforum_profile,
)

__all__ = [
    "BR_ANATEL_PRIVATIVA_PROFILE_ID",
    "CBRS_WINNFORUM_PROFILE_ID",
    "BandProfile",
    "ProtectionFlags",
    "br_anatel_privativa_profile",
    "cbrs_winnforum_profile",
]
