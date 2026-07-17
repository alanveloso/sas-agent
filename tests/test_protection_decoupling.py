"""Regression tests for band-profile-driven protection / GAA decoupling."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from domain.band_profile import (
    LEGACY_GAA_SOURCE_ID,
    ProtectionFlags,
    br_anatel_privativa_profile,
    cbrs_winnforum_profile,
)
from services.authorization_service import SOURCE_TYPE_GAA, ensure_gaa_authorization
from services.border_protection import violates_canadian_border_pfd
from services.cpas_service import _grant_conflicts_peer_esc
from services.federal_db_service import grant_blocked_by_fss_gwbl, heartbeat_federal_code


class GaaIdentityTests(unittest.TestCase):
    def test_cbrs_and_anatel_use_distinct_source_ids(self):
        cbrs = cbrs_winnforum_profile()
        anatel = br_anatel_privativa_profile()
        self.assertEqual(cbrs.gaa_source_id(), "FCC_PART_96_GAA")
        self.assertEqual(anatel.gaa_source_id(), "ANATEL_SLP_ACT_374_COEXISTENCE")
        self.assertNotEqual(cbrs.gaa_source_id(), anatel.gaa_source_id())

    def test_ensure_gaa_migrates_legacy_cbrs_row(self):
        db = MagicMock()
        legacy = SimpleNamespace(
            source_type=SOURCE_TYPE_GAA,
            source_id=LEGACY_GAA_SOURCE_ID,
            status="ACTIVE",
            expires_at=None,
            record_json="{}",
            updated_at=None,
        )
        grant = SimpleNamespace(
            authorization_source_type=SOURCE_TYPE_GAA,
            authorization_source_id=LEGACY_GAA_SOURCE_ID,
        )

        def filter_by(**kwargs):
            query = MagicMock()
            source_id = kwargs.get("source_id")
            auth_source_id = kwargs.get("authorization_source_id")
            if source_id == "FCC_PART_96_GAA":
                query.first.return_value = None
            elif source_id == LEGACY_GAA_SOURCE_ID:
                query.first.return_value = legacy
            elif auth_source_id == LEGACY_GAA_SOURCE_ID:
                query.all.return_value = [grant]
            else:
                query.first.return_value = None
                query.all.return_value = []
            return query

        db.query.return_value.filter_by.side_effect = filter_by
        row = ensure_gaa_authorization(db, profile=cbrs_winnforum_profile())
        self.assertIs(row, legacy)
        self.assertEqual(legacy.source_id, "FCC_PART_96_GAA")
        self.assertEqual(grant.authorization_source_id, "FCC_PART_96_GAA")


class ProtectionBypassTests(unittest.TestCase):
    def test_fss_gwbl_bypassed_when_flag_false(self):
        profile = replace(
            br_anatel_privativa_profile(),
            protection=ProtectionFlags(
                pal_gaa=False,
                fss_gwbl=False,
                exclusion_zones=False,
                border_protection=False,
                dpa=False,
                esc=False,
                quiet_zones=False,
            ),
            fss_gwbl_policy=None,
        )
        blocked = grant_blocked_by_fss_gwbl(
            MagicMock(),
            -15.0,
            -47.0,
            3_700_000_000,
            3_710_000_000,
            profile=profile,
        )
        self.assertFalse(blocked)

    def test_border_bypassed_for_anatel(self):
        profile = br_anatel_privativa_profile()
        self.assertFalse(
            violates_canadian_border_pfd(
                {"latitude": 49.0, "longitude": -122.0},
                20.0,
                3_650_000_000,
                3_660_000_000,
                profile=profile,
            )
        )

    def test_heartbeat_federal_skips_dpa_when_disabled(self):
        profile = br_anatel_privativa_profile()
        grant = SimpleNamespace(
            low_frequency=3_550_000_000,
            high_frequency=3_560_000_000,
            grant_json="{}",
        )
        cbsd = SimpleNamespace(
            registration_json=json.dumps(
                {"installationParam": {"latitude": 40.0, "longitude": -74.0}}
            )
        )
        with patch(
            "services.federal_db_service.get_sync_meta",
            return_value={"fss": 0, "gwbl": 0, "exz": 0, "dpa": 1},
        ), patch(
            "services.federal_db_service.fss_sites_near",
            return_value=[],
        ):
            code = heartbeat_federal_code(
                MagicMock(), cbsd, grant, profile=profile
            )
        self.assertIsNone(code)

    def test_esc_conflict_bypassed_when_esc_disabled(self):
        profile = br_anatel_privativa_profile()
        cbsd = SimpleNamespace()
        grant = SimpleNamespace(
            low_frequency=3_550_000_000,
            high_frequency=3_560_000_000,
        )
        self.assertFalse(
            _grant_conflicts_peer_esc(MagicMock(), cbsd, grant, profile=profile)
        )


class CbrsPolicyRegressionTests(unittest.TestCase):
    def test_cbrs_fss_policy_matches_legacy_subband(self):
        p = cbrs_winnforum_profile()
        assert p.fss_gwbl_policy is not None
        self.assertEqual(p.fss_gwbl_policy.low_hz, 3_650_000_000)
        self.assertEqual(p.fss_gwbl_policy.high_hz, 3_700_000_000)

    def test_cbrs_ntia_policy_matches_legacy_gbs(self):
        p = cbrs_winnforum_profile()
        assert p.ntia_exclusion_policy is not None
        self.assertEqual(p.ntia_exclusion_policy.low_hz, 3_550_000_000)
        self.assertEqual(p.ntia_exclusion_policy.high_hz, 3_650_000_000)


if __name__ == "__main__":
    unittest.main()
