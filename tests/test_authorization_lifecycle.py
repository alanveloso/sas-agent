"""Unit/integration tests for the generic authorization lifecycle."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


def _fresh_db_env():
    """Point SAS_DATABASE_PATH at a temporary SQLite file before importing app modules."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["SAS_DATABASE_PATH"] = path
    # Force re-import of database-bound modules with the new path.
    import importlib
    import sys

    for mod in list(sys.modules):
        if mod in {
            "database",
            "models",
            "models.models",
            "domain",
            "domain.band_profile",
            "services.authorization_service",
            "services.pal_service",
            "services.grant_service",
            "services.heartbeat_service",
            "services.spectrum_inquiry_service",
            "services.profile_manager",
            "services.meas_report",
            "services.registration_service",
        } or mod.startswith("models.") or mod.startswith("services.") or mod.startswith("domain."):
            del sys.modules[mod]

    import database

    importlib.reload(database)
    database.init_db()
    return path, database


class AuthorizationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.db_path, self.database = _fresh_db_env()
        from models.models import Cbsd, FccIdRecord, Grant, UserIdRecord
        from services.authorization_service import ensure_gaa_authorization
        from services.pal_service import upsert_pal_records
        from services.grant_service import process_grant
        from services.heartbeat_service import process_heartbeat

        self.Cbsd = Cbsd
        self.FccIdRecord = FccIdRecord
        self.Grant = Grant
        self.UserIdRecord = UserIdRecord
        self.upsert_pal_records = upsert_pal_records
        self.process_grant = process_grant
        self.process_heartbeat = process_heartbeat
        self.ensure_gaa_authorization = ensure_gaa_authorization

        self.db = self.database.SessionLocal()
        self.ensure_gaa_authorization(self.db)
        self.db.add(self.FccIdRecord(fcc_id="test_fcc", fcc_max_eirp=47.0))
        self.db.add(self.UserIdRecord(user_id="operator_a"))
        self.cbsd = self.Cbsd(
            cbsd_id="cbsd/test_fcc/serial1",
            fcc_id="test_fcc",
            user_id="operator_a",
            cbsd_serial_number="serial1",
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
                        "antennaGain": 0,
                    },
                }
            ),
        )
        self.db.add(self.cbsd)
        self.db.commit()

        self.pal_id = "pal/test/1"
        self.future_exp = (datetime.utcnow() + timedelta(days=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self._inject_pal(self.pal_id, status="VALID", expiration=self.future_exp)
        self._inject_ppa(self.pal_id, self.cbsd.cbsd_id)

    def tearDown(self):
        self.db.close()
        try:
            Path(self.db_path).unlink(missing_ok=True)
        except OSError:
            pass

    def _inject_pal(self, pal_id: str, *, status: str, expiration: str):
        from models.models import AdminInjectedData

        record = {
            "palId": pal_id,
            "userId": "operator_a",
            "licenseStatus": status,
            "license": {"licenseExpiration": expiration},
            "channelAssignment": {
                "primaryAssignment": {
                    "lowFrequency": 3550000000,
                    "highFrequency": 3560000000,
                }
            },
        }
        self.upsert_pal_records(self.db, record)
        # Also keep a zone PPA linkage via AdminInjectedData.
        return record

    def _inject_ppa(self, pal_id: str, cbsd_id: str):
        from models.models import AdminInjectedData

        # Replace any prior PPA row for deterministic tests.
        self.db.query(AdminInjectedData).filter_by(kind="zone").delete()
        zone = {
            "record": {
                "usage": "PPA",
                "ppaInfo": {
                    "palId": [pal_id],
                    "cbsdReferenceId": [cbsd_id],
                },
                "zone": {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [
                                    [
                                        [-123.0, 36.0],
                                        [-121.0, 36.0],
                                        [-121.0, 38.0],
                                        [-123.0, 38.0],
                                        [-123.0, 36.0],
                                    ]
                                ],
                            },
                            "properties": {},
                        }
                    ],
                },
            }
        }
        self.db.add(AdminInjectedData(kind="zone", data_json=json.dumps(zone)))
        self.db.commit()

    def _grant_req(self, *, low=3550000000, high=3560000000):
        return {
            "cbsdId": self.cbsd.cbsd_id,
            "operationParam": {
                "maxEirp": 20.0,
                "operationFrequencyRange": {
                    "lowFrequency": low,
                    "highFrequency": high,
                },
            },
        }

    def _issue_pal_grant(self):
        responses = self.process_grant(self.db, [self._grant_req()])
        self.assertEqual(responses[0]["response"]["responseCode"], 0, responses)
        grant_id = responses[0]["grantId"]
        grant = self.db.query(self.Grant).filter_by(grant_id=grant_id).one()
        return grant

    def _issue_gaa_grant(self):
        # Use a frequency outside the PAL block so channel resolves to GAA.
        responses = self.process_grant(
            self.db,
            [self._grant_req(low=3600000000, high=3610000000)],
        )
        self.assertEqual(responses[0]["response"]["responseCode"], 0, responses)
        return self.db.query(self.Grant).filter_by(
            grant_id=responses[0]["grantId"]
        ).one()

    def test_pal_grant_binds_authorization_source(self):
        grant = self._issue_pal_grant()
        self.assertEqual(grant.authorization_source_type, "PAL")
        self.assertEqual(grant.authorization_source_id, self.pal_id)
        self.assertIsNotNone(grant.authorization_expires_at)

    def test_gaa_grant_binds_global_policy(self):
        grant = self._issue_gaa_grant()
        self.assertEqual(grant.authorization_source_type, "GAA")
        self.assertEqual(grant.authorization_source_id, "CBRS")

    def test_revocation_cascade_terminates_derived_grants(self):
        grant = self._issue_pal_grant()
        past = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._inject_pal(self.pal_id, status="EXPIRED", expiration=past)
        self.db.refresh(grant)
        self.assertTrue(grant.terminated)
        self.assertIn(grant.termination_reason, {
            "AUTHORIZATION_REVOKED",
            "AUTHORIZATION_EXPIRED",
        })

    def test_heartbeat_returns_500_after_revocation(self):
        grant = self._issue_pal_grant()
        # Authorize first heartbeat while source is still valid.
        hb1 = self.process_heartbeat(
            self.db,
            [
                {
                    "cbsdId": self.cbsd.cbsd_id,
                    "grantId": grant.grant_id,
                    "operationState": "GRANTED",
                }
            ],
        )
        self.assertEqual(hb1[0]["response"]["responseCode"], 0, hb1)

        past = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._inject_pal(self.pal_id, status="REVOKED", expiration=past)

        hb2 = self.process_heartbeat(
            self.db,
            [
                {
                    "cbsdId": self.cbsd.cbsd_id,
                    "grantId": grant.grant_id,
                    "operationState": "AUTHORIZED",
                }
            ],
        )
        self.assertEqual(hb2[0]["response"]["responseCode"], 500, hb2)
        self.assertIn("transmitExpireTime", hb2[0])
        tx = datetime.strptime(hb2[0]["transmitExpireTime"], "%Y-%m-%dT%H:%M:%SZ")
        self.assertLess(tx, datetime.utcnow())

    def test_heartbeat_renewal_never_exceeds_authorization_expires_at(self):
        near = datetime.utcnow() + timedelta(seconds=120)
        near_s = near.strftime("%Y-%m-%dT%H:%M:%SZ")
        self._inject_pal(self.pal_id, status="VALID", expiration=near_s)
        grant = self._issue_pal_grant()

        hb = self.process_heartbeat(
            self.db,
            [
                {
                    "cbsdId": self.cbsd.cbsd_id,
                    "grantId": grant.grant_id,
                    "operationState": "GRANTED",
                    "grantRenew": True,
                }
            ],
        )
        self.assertEqual(hb[0]["response"]["responseCode"], 0, hb)
        renewed = datetime.strptime(hb[0]["grantExpireTime"], "%Y-%m-%dT%H:%M:%SZ")
        self.assertLessEqual(renewed, near.replace(microsecond=0))
        tx = datetime.strptime(hb[0]["transmitExpireTime"], "%Y-%m-%dT%H:%M:%SZ")
        self.assertLessEqual(tx, near.replace(microsecond=0))

    def test_missing_authorization_source_is_fail_closed(self):
        grant = self._issue_pal_grant()
        from models.models import AuthorizationSource

        self.db.query(AuthorizationSource).filter_by(
            source_type="PAL", source_id=self.pal_id
        ).delete()
        # Avoid cascade marking via upsert; leave grant pointing at missing source.
        grant.terminated = False
        grant.termination_reason = None
        self.db.commit()

        hb = self.process_heartbeat(
            self.db,
            [
                {
                    "cbsdId": self.cbsd.cbsd_id,
                    "grantId": grant.grant_id,
                    "operationState": "GRANTED",
                }
            ],
        )
        self.assertEqual(hb[0]["response"]["responseCode"], 500, hb)

    def test_revocation_isolates_other_pal_grants(self):
        other_pal = "pal/test/2"
        other_exp = (datetime.utcnow() + timedelta(days=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        # Second PAL on a different 10 MHz block.
        self.upsert_pal_records(
            self.db,
            {
                "palId": other_pal,
                "userId": "operator_a",
                "licenseStatus": "VALID",
                "license": {"licenseExpiration": other_exp},
                "channelAssignment": {
                    "primaryAssignment": {
                        "lowFrequency": 3560000000,
                        "highFrequency": 3570000000,
                    }
                },
            },
        )
        # Expand PPA to cover both PALs.
        from models.models import AdminInjectedData

        self.db.query(AdminInjectedData).filter_by(kind="zone").delete()
        zone = {
            "record": {
                "usage": "PPA",
                "ppaInfo": {
                    "palId": [self.pal_id, other_pal],
                    "cbsdReferenceId": [self.cbsd.cbsd_id],
                },
                "zone": {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [
                                    [
                                        [-123.0, 36.0],
                                        [-121.0, 36.0],
                                        [-121.0, 38.0],
                                        [-123.0, 38.0],
                                        [-123.0, 36.0],
                                    ]
                                ],
                            },
                            "properties": {},
                        }
                    ],
                },
            }
        }
        self.db.add(AdminInjectedData(kind="zone", data_json=json.dumps(zone)))
        self.db.commit()

        g1 = self._issue_pal_grant()
        responses = self.process_grant(
            self.db,
            [self._grant_req(low=3560000000, high=3570000000)],
        )
        self.assertEqual(responses[0]["response"]["responseCode"], 0, responses)
        g2 = self.db.query(self.Grant).filter_by(grant_id=responses[0]["grantId"]).one()
        self.assertEqual(g2.authorization_source_id, other_pal)

        past = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._inject_pal(self.pal_id, status="REVOKED", expiration=past)
        self.db.refresh(g1)
        self.db.refresh(g2)
        self.assertTrue(g1.terminated)
        self.assertFalse(g2.terminated)


if __name__ == "__main__":
    unittest.main()
