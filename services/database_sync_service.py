"""Pull external PAL / CPI / FSS databases injected via /admin/injectdata/database_url."""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import ssl
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

from models.models import AdminInjectedData, CpiUser

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
HARNESS_CERTS = ROOT.parent / "src" / "harness" / "certs"
CA_CERT = HARNESS_CERTS / "ca.cert"

DB_BASIC_AUTH = ("username", "password")


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    if CA_CERT.is_file():
        ctx.load_verify_locations(cafile=str(CA_CERT))
    return ctx


def _http_get(url: str, *, auth: bool = False) -> bytes:
    kwargs: dict[str, Any] = {"verify": _ssl_context(), "timeout": 30.0}
    if auth:
        kwargs["auth"] = DB_BASIC_AUTH
    with httpx.Client(**kwargs) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content


def _store_injection(db: Session, kind: str, payload: Any) -> None:
    db.add(
        AdminInjectedData(
            kind=kind,
            data_json=json.dumps(payload if payload is not None else {}),
        )
    )


def sync_injected_database_urls(db: Session, *, profile=None) -> None:
    """Fetch every injected database_url during CPAS / daily activities."""
    from services.profile_manager import resolve_optional

    active = resolve_optional(profile)
    rows = list(db.query(AdminInjectedData).filter_by(kind="database_url").all())
    for row in rows:
        try:
            meta = json.loads(row.data_json or "{}")
        except json.JSONDecodeError:
            continue
        db_type = (meta.get("type") or "").upper()
        url = meta.get("url") or ""
        if not url:
            continue
        try:
            if db_type == "PAL":
                if not active.protection.pal_gaa:
                    continue
                _sync_pal(db, url)
            elif db_type == "CPI":
                _sync_cpi(db, url)
            elif db_type == "EXCLUSION_ZONE":
                if not active.protection.exclusion_zones:
                    continue
                body = _http_get(url, auth=False)
                _apply_exclusion_zone_kml(db, body, profile=active)
            elif db_type == "SCHEDULED_DPA":
                if not active.protection.dpa:
                    continue
                body = _http_get(url, auth=False)
                db.query(AdminInjectedData).filter_by(kind="scheduled_dpa").delete()
                _store_injection(
                    db,
                    "scheduled_dpa",
                    {"raw": body.decode("utf-8", errors="replace")},
                )
                from services.federal_db_service import bump_sync_meta

                bump_sync_meta(db, "dpa")
            elif db_type == "FSS":
                if not active.protection.fss_gwbl:
                    continue
                body = _http_get(url, auth=True)
                payload = json.loads(body.decode("utf-8"))
                from services.federal_db_service import replace_fss_from_federal_payload

                replace_fss_from_federal_payload(db, payload)
            elif db_type == "GWBL":
                if not active.protection.fss_gwbl:
                    continue
                body = _http_get(url, auth=False)
                from services.federal_db_service import replace_gwbl_from_zip

                replace_gwbl_from_zip(db, body)
        except Exception:
            logger.exception("Failed syncing database_url type=%s url=%s", db_type, url)

    db.commit()


def _parse_freq_range_mhz(text: str | None) -> list[dict[str, int]]:
    """Parse '3600-3650' into Hz ranges."""
    if not text:
        return []
    out: list[dict[str, int]] = []
    for part in re.split(r"[;,]", text):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*$", part)
        if not m:
            continue
        lo_mhz, hi_mhz = float(m.group(1)), float(m.group(2))
        out.append(
            {
                "lowFrequency": int(lo_mhz * 1_000_000),
                "highFrequency": int(hi_mhz * 1_000_000),
            }
        )
    return out


def _parse_kml_ring(text: str | None) -> list[list[float]]:
    ring: list[list[float]] = []
    for tok in (text or "").split():
        parts = tok.split(",")
        if len(parts) >= 2:
            try:
                ring.append([float(parts[0]), float(parts[1])])
            except ValueError:
                continue
    if len(ring) >= 3 and ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    return ring


def _apply_exclusion_zone_kml(db: Session, body: bytes, *, profile=None) -> None:
    """Replace exclusion_zone records with polygons parsed from federal EXZ KML."""
    from services.exclusion_zone_service import KIND_EXCLUSION_ZONE
    from services.federal_db_service import bump_sync_meta
    from services.profile_manager import resolve_optional

    active = resolve_optional(profile)
    text = body.decode("utf-8", errors="replace")
    root = ET.fromstring(text)
    db.query(AdminInjectedData).filter_by(kind=KIND_EXCLUSION_ZONE).delete()

    fallback_ranges: list[dict[str, int]] = []
    if active.ntia_exclusion_policy is not None:
        fallback_ranges = [
            {
                "lowFrequency": active.ntia_exclusion_policy.low_hz,
                "highFrequency": active.ntia_exclusion_policy.high_hz,
            }
        ]
    else:
        fallback_ranges = [
            {
                "lowFrequency": active.band_low_hz,
                "highFrequency": active.band_high_hz,
            }
        ]

    for pm in root.iter("{http://www.opengis.net/kml/2.2}Placemark"):
        coords_el = pm.find(
            ".//{http://www.opengis.net/kml/2.2}outerBoundaryIs"
            "/{http://www.opengis.net/kml/2.2}LinearRing"
            "/{http://www.opengis.net/kml/2.2}coordinates"
        )
        ring = _parse_kml_ring(coords_el.text if coords_el is not None else None)
        if len(ring) < 4:
            continue
        freq_text = None
        for data in pm.findall(".//{http://www.opengis.net/kml/2.2}Data"):
            if data.get("name") == "freqRangeMhz":
                val = data.find("{http://www.opengis.net/kml/2.2}value")
                if val is not None:
                    freq_text = val.text
        freq_ranges = _parse_freq_range_mhz(freq_text)
        if not freq_ranges:
            freq_ranges = list(fallback_ranges)
        payload = {
            "zone": {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Polygon", "coordinates": [ring]},
                        "properties": {},
                    }
                ],
            },
            "frequencyRanges": freq_ranges,
        }
        _store_injection(db, KIND_EXCLUSION_ZONE, payload)

    bump_sync_meta(db, "exz")


def _sync_pal(db: Session, url: str) -> None:
    from services.pal_service import upsert_pal_records

    body = _http_get(url, auth=False)
    records = json.loads(body.decode("utf-8"))
    upsert_pal_records(db, records)


def _sync_cpi(db: Session, index_url: str) -> None:
    raw = _http_get(index_url, auth=False).decode("utf-8")
    reader = csv.DictReader(io.StringIO(raw))
    for row in reader:
        cpi_id = (row.get("cpiId") or "").strip()
        status = (row.get("status") or "").strip().upper()
        key_url = (row.get("publicKeyIdentifier") or "").strip()
        if not cpi_id or status != "ACTIVE" or not key_url:
            continue
        try:
            public_key = _http_get(key_url, auth=False).decode("utf-8")
        except Exception:
            logger.exception("Failed fetching CPI public key %s", key_url)
            continue
        existing = db.query(CpiUser).filter_by(cpi_id=cpi_id).first()
        if existing:
            existing.cpi_public_key = public_key
            existing.cpi_name = existing.cpi_name or cpi_id
        else:
            db.add(
                CpiUser(
                    cpi_id=cpi_id,
                    cpi_name=cpi_id,
                    cpi_public_key=public_key,
                )
            )
