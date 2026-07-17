"""Unit tests for BandProfile, ProfileManager, and RF helpers driven by profiles."""

from __future__ import annotations

import os
import unittest
from dataclasses import FrozenInstanceError

from domain.band_profile import (
    BR_ANATEL_PRIVATIVA_PROFILE_ID,
    CBRS_WINNFORUM_PROFILE_ID,
    BandProfile,
    EscPolicy,
    FssGwblPolicy,
    ProtectionFlags,
    br_anatel_privativa_profile,
    cbrs_winnforum_profile,
    profile_from_mapping,
)
from services import profile_manager as pm


class BandProfileModelTests(unittest.TestCase):
    def test_cbrs_defaults_match_legacy(self):
        p = cbrs_winnforum_profile()
        self.assertEqual(p.profile_id, CBRS_WINNFORUM_PROFILE_ID)
        self.assertEqual(p.band_low_hz, 3_550_000_000)
        self.assertEqual(p.band_high_hz, 3_700_000_000)
        self.assertEqual(p.channel_width_hz, 10_000_000)
        self.assertEqual(p.regulatory_rule_id, "FCC_PART_96")
        self.assertEqual(p.gaa_policy_id, "FCC_PART_96_GAA")
        self.assertEqual(p.gaa_source_id(), "FCC_PART_96_GAA")
        self.assertEqual(p.channel_count, 15)
        self.assertEqual(p.min_without_grant_reports, 15)
        self.assertEqual(p.max_allowed_eirp_mhz("A", fcc_max_eirp=47.0), 20.0)
        self.assertEqual(p.max_allowed_eirp_mhz("B", fcc_max_eirp=47.0), 37.0)
        self.assertEqual(
            p.max_allowed_eirp_mhz("A", fcc_max_eirp=47.0, eirp_capability=20.0),
            10.0,
        )
        self.assertIsNotNone(p.esc_policy)
        assert p.esc_policy is not None
        self.assertEqual(p.esc_policy.low_hz, 3_550_000_000)
        self.assertEqual(p.esc_policy.high_hz, 3_700_000_000)
        self.assertEqual(p.esc_policy.protection_m, 40_000.0)
        assert p.fss_gwbl_policy is not None
        self.assertEqual(p.fss_gwbl_policy.low_hz, 3_650_000_000)
        self.assertEqual(p.fss_gwbl_policy.high_hz, 3_700_000_000)
        assert p.ntia_exclusion_policy is not None
        self.assertEqual(p.ntia_exclusion_policy.low_hz, 3_550_000_000)
        self.assertEqual(p.ntia_exclusion_policy.high_hz, 3_650_000_000)
        assert p.dpa_policy is not None
        self.assertEqual(p.dpa_policy.low_hz, 3_500_000_000)
        assert p.border_policy is not None
        self.assertEqual(p.border_policy.itm_freq_mhz, 3625.0)

    def test_br_anatel_privativa_profile(self):
        p = br_anatel_privativa_profile()
        self.assertEqual(p.profile_id, BR_ANATEL_PRIVATIVA_PROFILE_ID)
        self.assertEqual(p.band_low_hz, 3_700_000_000)
        self.assertEqual(p.band_high_hz, 3_800_000_000)
        self.assertEqual(p.channel_width_hz, 10_000_000)
        self.assertEqual(p.regulatory_rule_id, "ANATEL_SLP_ACT_374")
        self.assertEqual(p.gaa_source_id(), "ANATEL_SLP_ACT_374_COEXISTENCE")
        self.assertEqual(p.channel_count, 10)
        self.assertEqual(p.category_a_max_eirp_dbm, 47.0)
        self.assertEqual(p.category_b_max_eirp_dbm, 47.0)
        self.assertEqual(p.eirp_reference_bandwidth_hz, 100_000_000)
        self.assertFalse(p.protection.esc)
        self.assertFalse(p.protection.dpa)
        self.assertFalse(p.protection.border_protection)
        self.assertTrue(p.protection.fss_gwbl)
        self.assertTrue(p.protection.exclusion_zones)
        self.assertIsNone(p.esc_policy)
        self.assertIsNone(p.dpa_policy)
        self.assertIsNone(p.border_policy)
        assert p.fss_gwbl_policy is not None
        self.assertEqual(p.fss_gwbl_policy.low_hz, 3_700_000_000)
        self.assertEqual(p.fss_gwbl_policy.high_hz, 3_800_000_000)

    def test_immutable(self):
        p = cbrs_winnforum_profile()
        with self.assertRaises(FrozenInstanceError):
            p.channel_width_hz = 5_000_000  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            p.protection.pal_gaa = False  # type: ignore[misc]

    def test_invalid_band_rejected(self):
        with self.assertRaises(ValueError):
            BandProfile(
                profile_id="bad",
                band_low_hz=100,
                band_high_hz=50,
                channel_width_hz=10,
                regulatory_rule_id="X",
            )

    def test_alternate_raster(self):
        p = BandProfile(
            profile_id="exp_5mhz",
            band_low_hz=3_700_000_000,
            band_high_hz=3_800_000_000,
            channel_width_hz=5_000_000,
            regulatory_rule_id="EXPERIMENTAL",
            raster_origin_hz=3_700_000_000,
            protection=ProtectionFlags(pal_gaa=False),
        )
        chans = p.split_channels(3_700_000_000, 3_720_000_000)
        self.assertEqual(
            chans,
            [
                (3_700_000_000, 3_705_000_000),
                (3_705_000_000, 3_710_000_000),
                (3_710_000_000, 3_715_000_000),
                (3_715_000_000, 3_720_000_000),
            ],
        )
        self.assertEqual(p.channel_count, 20)

    def test_profile_from_mapping(self):
        p = profile_from_mapping(
            {
                "profile_id": "custom",
                "band_low_hz": 3_400_000_000,
                "band_high_hz": 3_500_000_000,
                "channel_width_hz": 20_000_000,
                "regulatory_rule_id": "CUSTOM",
                "protection": {"pal_gaa": False, "fss_gwbl": False},
            }
        )
        self.assertEqual(p.profile_id, "custom")
        self.assertFalse(p.protection.pal_gaa)
        self.assertEqual(p.gaa_policy_id, "CUSTOM_GAA")
        self.assertIsNone(p.fss_gwbl_policy)

    def test_profile_from_mapping_with_policies(self):
        p = profile_from_mapping(
            {
                "profile_id": "custom_esc",
                "band_low_hz": 3_500_000_000,
                "band_high_hz": 3_600_000_000,
                "channel_width_hz": 10_000_000,
                "regulatory_rule_id": "CUSTOM",
                "protection": {"esc": True, "fss_gwbl": True},
                "esc_policy": {
                    "low_hz": 3_500_000_000,
                    "high_hz": 3_560_000_000,
                    "protection_m": 25_000.0,
                },
                "fss_gwbl_policy": {
                    "low_hz": 3_550_000_000,
                    "high_hz": 3_600_000_000,
                },
            }
        )
        self.assertIsInstance(p.esc_policy, EscPolicy)
        assert p.esc_policy is not None
        self.assertEqual(p.esc_policy.protection_m, 25_000.0)
        self.assertIsInstance(p.fss_gwbl_policy, FssGwblPolicy)
        assert p.fss_gwbl_policy is not None
        self.assertEqual(p.fss_gwbl_policy.low_hz, 3_550_000_000)


class ProfileManagerTests(unittest.TestCase):
    def setUp(self):
        pm.reset_active_band_profile()
        self._env = {
            k: os.environ.get(k)
            for k in ("BAND_PROFILE", "BAND_PROFILE_FILE", "BAND_LOW", "BAND_HIGH")
        }

    def tearDown(self):
        pm.reset_active_band_profile()
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_default_is_cbrs(self):
        os.environ.pop("BAND_PROFILE", None)
        os.environ.pop("BAND_PROFILE_FILE", None)
        profile = pm.initialize_active_profile(force=True)
        self.assertEqual(profile.profile_id, CBRS_WINNFORUM_PROFILE_ID)

    def test_anatel_profile_selected_from_environment(self):
        os.environ["BAND_PROFILE"] = BR_ANATEL_PRIVATIVA_PROFILE_ID
        os.environ.pop("BAND_LOW", None)
        os.environ.pop("BAND_HIGH", None)
        profile = pm.initialize_active_profile(force=True)
        self.assertEqual(profile.profile_id, BR_ANATEL_PRIVATIVA_PROFILE_ID)
        self.assertEqual(profile.band_low_hz, 3_700_000_000)
        self.assertEqual(profile.band_high_hz, 3_800_000_000)
        self.assertIn(BR_ANATEL_PRIVATIVA_PROFILE_ID, pm.list_profile_ids())

    def test_unknown_profile_raises(self):
        with self.assertRaises(KeyError):
            pm.resolve_profile("does_not_exist")

    def test_band_env_override(self):
        os.environ["BAND_PROFILE"] = CBRS_WINNFORUM_PROFILE_ID
        os.environ["BAND_LOW"] = "3560000000"
        os.environ["BAND_HIGH"] = "3680000000"
        profile = pm.initialize_active_profile(force=True)
        self.assertEqual(profile.band_low_hz, 3_560_000_000)
        self.assertEqual(profile.band_high_hz, 3_680_000_000)
        self.assertEqual(profile.channel_width_hz, 10_000_000)

    def test_resolve_optional_fallback(self):
        pm.initialize_active_profile(force=True)
        active = pm.get_active_band_profile()
        self.assertIs(pm.resolve_optional(None).profile_id, active.profile_id)
        custom = BandProfile(
            profile_id="tmp",
            band_low_hz=1,
            band_high_hz=2,
            channel_width_hz=1,
            regulatory_rule_id="TMP",
        )
        self.assertEqual(pm.resolve_optional(custom).profile_id, "tmp")

    def test_register_custom_profile(self):
        def factory():
            return BandProfile(
                profile_id="lab_band",
                band_low_hz=3_800_000_000,
                band_high_hz=3_900_000_000,
                channel_width_hz=20_000_000,
                regulatory_rule_id="LAB",
            )

        pm.register_profile("lab_band", factory)
        self.assertIn("lab_band", pm.list_profile_ids())
        os.environ["BAND_PROFILE"] = "lab_band"
        os.environ.pop("BAND_PROFILE_FILE", None)
        os.environ.pop("BAND_LOW", None)
        os.environ.pop("BAND_HIGH", None)
        profile = pm.initialize_active_profile(force=True)
        self.assertEqual(profile.channel_width_hz, 20_000_000)

    def test_profile_file_env_takes_precedence(self):
        import json
        import tempfile

        payload = {
            "profile_id": "from_file",
            "band_low_hz": 3_800_000_000,
            "band_high_hz": 3_900_000_000,
            "channel_width_hz": 20_000_000,
            "regulatory_rule_id": "FILE_CUSTOM",
            "protection": {"pal_gaa": False, "esc": False},
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as handle:
            json.dump(payload, handle)
            path = handle.name
        try:
            os.environ["BAND_PROFILE"] = CBRS_WINNFORUM_PROFILE_ID
            os.environ["BAND_PROFILE_FILE"] = path
            os.environ.pop("BAND_LOW", None)
            os.environ.pop("BAND_HIGH", None)
            profile = pm.initialize_active_profile(force=True)
            self.assertEqual(profile.profile_id, "from_file")
            self.assertEqual(profile.channel_width_hz, 20_000_000)
            self.assertEqual(profile.regulatory_rule_id, "FILE_CUSTOM")
            self.assertIn("from_file", pm.list_profile_ids())
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_missing_profile_file_raises(self):
        os.environ["BAND_PROFILE_FILE"] = "/tmp/does-not-exist-band-profile.json"
        with self.assertRaises(FileNotFoundError):
            pm.initialize_active_profile(force=True)


class MeasReportProfileTests(unittest.TestCase):
    def test_validate_uses_profile_bandwidth(self):
        from services.meas_report import validate_meas_report

        narrow = BandProfile(
            profile_id="narrow",
            band_low_hz=3_550_000_000,
            band_high_hz=3_560_000_000,
            channel_width_hz=5_000_000,
            regulatory_rule_id="LAB",
        )
        ok_report = {
            "rcvdPowerMeasReports": [
                {
                    "measFrequency": 3_550_000_000,
                    "measBandwidth": 5_000_000,
                    "measRcvdPower": -80,
                },
                {
                    "measFrequency": 3_555_000_000,
                    "measBandwidth": 5_000_000,
                    "measRcvdPower": -80,
                },
            ]
        }
        self.assertIsNone(
            validate_meas_report(
                ok_report, require_full_cbrs=True, profile=narrow
            )
        )
        bad_bw = {
            "rcvdPowerMeasReports": [
                {
                    "measFrequency": 3_550_000_000,
                    "measBandwidth": 10_000_000,
                    "measRcvdPower": -80,
                }
            ]
        }
        self.assertEqual(
            validate_meas_report(bad_bw, profile=narrow),
            103,
        )


class ServiceInjectionTests(unittest.TestCase):
    def test_spectrum_inquiry_accepts_explicit_profile(self):
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.environ["SAS_DATABASE_PATH"] = path
        import importlib
        import json
        import sys

        for mod in list(sys.modules):
            if (
                mod in {"database", "models", "models.models"}
                or mod.startswith("models.")
                or mod.startswith("services.")
            ):
                del sys.modules[mod]

        import database

        importlib.reload(database)
        database.init_db()
        from models.models import Cbsd, FccIdRecord, UserIdRecord
        from services.spectrum_inquiry_service import process_spectrum_inquiry

        db = database.SessionLocal()
        try:
            db.add(FccIdRecord(fcc_id="fcc1", fcc_max_eirp=47.0))
            db.add(UserIdRecord(user_id="u1"))
            db.add(
                Cbsd(
                    cbsd_id="fcc1/s1",
                    fcc_id="fcc1",
                    user_id="u1",
                    cbsd_serial_number="s1",
                    cbsd_category="A",
                    registration_json=json.dumps(
                        {
                            "cbsdCategory": "A",
                            "installationParam": {
                                "latitude": 37.0,
                                "longitude": -122.0,
                                "height": 6.0,
                                "heightType": "AGL",
                                "indoorDeployment": True,
                            },
                        }
                    ),
                )
            )
            db.commit()
            profile = cbrs_winnforum_profile()
            resp = process_spectrum_inquiry(
                db,
                [
                    {
                        "cbsdId": "fcc1/s1",
                        "inquiredSpectrum": [
                            {
                                "lowFrequency": 3_550_000_000,
                                "highFrequency": 3_560_000_000,
                            }
                        ],
                    }
                ],
                profile=profile,
            )
            self.assertEqual(resp[0]["response"]["responseCode"], 0)
            channels = resp[0]["availableChannel"]
            self.assertTrue(channels)
            self.assertEqual(channels[0]["ruleApplied"], "FCC_PART_96")
            self.assertEqual(channels[0]["channelType"], "GAA")
            # 10 MHz channel from profile
            fr = channels[0]["frequencyRange"]
            self.assertEqual(
                fr["highFrequency"] - fr["lowFrequency"], profile.channel_width_hz
            )
        finally:
            db.close()
            try:
                os.unlink(path)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
