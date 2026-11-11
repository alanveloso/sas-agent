# API Reference

This page describes the interfaces exposed by `sas-agent` and separates the
three system actors:

- **CBSD–SAS:** device lifecycle and Grant management;
- **Control Plane–SAS:** registrations, authorizations, revocations, and
  administrative activities;
- **SAS–SAS:** publication and synchronization of Full Activity Dumps (FAD).

## Conventions

- Interfaces are HTTPS with mTLS client authentication.
- The RSA listener uses port `9000` and the ECDSA listener uses port `9001`.
- CBSD–SAS APIs follow WInnForum version `v1.2`; SAS–SAS uses `v1.3`.
- CBSD–SAS requests accept at most 100 items per batch.
- CBSD protocol errors normally return HTTP 200 and report the outcome in
  `response.responseCode`.
- The administrative API (`/admin/*`) additionally requires that the client
  certificate validated by the TLS handshake has a SHA-256 fingerprint listed
  in `SAS_ADMIN_CERT_SHA256` (comma separated). Otherwise it returns HTTP 403;
  with the variable unset, all administrative calls are rejected. HTTP headers
  are never used as client identity.
- The band profile is selected at startup with `BAND_PROFILE_FILE` or
  `BAND_PROFILE`. There is no endpoint to change the profile at runtime.
- One instance manages one profile/band at a time. For multiple bands, run
  separate instances with their own ports and databases.

## Common CBSD–SAS response codes

| Code | Meaning |
|---:|---|
| `0` | Operation completed successfully |
| `100` | Unsupported protocol version |
| `101` | FCC ID is blacklisted |
| `102` | Required parameter missing |
| `103` | Invalid value or identifier |
| `200` | Registration pending |
| `300` | Spectrum outside the supported band |
| `400` | Interference or regulatory protection |
| `401` | Conflict with an existing Grant |
| `500` | Grant terminated |
| `501` | Grant suspended |
| `502` | Operational state out of sync |

## CBSD–SAS

### Registration

`POST /v1.2/registration`

Registers or updates one or more CBSDs. The essential fields on each item are
`userId`, `fccId`, and `cbsdSerialNumber`. Category, installation, CPI data,
conditionals, and measurement reports may also be supplied.

```json
{
  "registrationRequest": [
    {
      "userId": "operator_a",
      "fccId": "fcc/example",
      "cbsdSerialNumber": "device-001",
      "cbsdCategory": "A",
      "airInterface": {"radioTechnology": "E_UTRA"},
      "installationParam": {
        "latitude": -15.8,
        "longitude": -47.9,
        "height": 10,
        "heightType": "AGL",
        "indoorDeployment": true,
        "antennaAzimuth": 0,
        "antennaDowntilt": 0,
        "antennaGain": 0,
        "antennaBeamwidth": 360
      }
    }
  ]
}
```

Response:

```json
{
  "registrationResponse": [
    {
      "cbsdId": "cbsd/...",
      "response": {"responseCode": 0}
    }
  ]
}
```

Registration persists the CBSD and the client-certificate fingerprint.
Re-registration updates the stored data and terminates previous Grants for that
device.

### Spectrum Inquiry

`POST /v1.2/spectrumInquiry`

Queries available channels inside the active profile band. Evaluation considers
PAL/PPA, WISP, FSS, exclusion zones, and other protections enabled in the
`BandProfile`.

```json
{
  "spectrumInquiryRequest": [
    {
      "cbsdId": "cbsd/...",
      "inquiredSpectrum": [
        {
          "lowFrequency": 3700000000,
          "highFrequency": 3710000000
        }
      ]
    }
  ]
}
```

The response includes `availableChannel`, `channelType` (`PAL` or `GAA`), and
`ruleApplied`. This operation does not change persistence.

### Grant request

`POST /v1.2/grant`

Creates an operational authorization for a given frequency range and power.

```json
{
  "grantRequest": [
    {
      "cbsdId": "cbsd/...",
      "operationParam": {
        "maxEirp": 20,
        "operationFrequencyRange": {
          "lowFrequency": 3700000000,
          "highFrequency": 3710000000
        }
      }
    }
  ]
}
```

On success, the response contains `grantId`, `grantExpireTime`,
`heartbeatInterval`, and `channelType`. The Grant is bound to its
`AuthorizationSource`:

- `PAL`: `source_id` is the `palId`;
- `GAA`/coexistence: `source_id` comes from the active profile's
  `gaa_policy_id`.

A PAL Grant requires an active, non-expired license bound to the user and PPA.
Its validity never exceeds the validity of the originating authorization.

### Heartbeat

`POST /v1.2/heartbeat`

Keeps a Grant active, renews its validity when allowed, and applies
revocations or new restrictions.

```json
{
  "heartbeatRequest": [
    {
      "cbsdId": "cbsd/...",
      "grantId": "grant/...",
      "operationState": "GRANTED",
      "grantRenew": true
    }
  ]
}
```

Every response includes `transmitExpireTime`. A revoked or expired
authorization returns code `500`, with `transmitExpireTime` in the past.

### Relinquishment

`POST /v1.2/relinquishment`

Allows the CBSD to terminate a specific Grant.

```json
{
  "relinquishmentRequest": [
    {"cbsdId": "cbsd/...", "grantId": "grant/..."}
  ]
}
```

The Grant is marked terminated, but its database row remains.

### Deregistration

`POST /v1.2/deregistration`

Removes the CBSD registration and invalidates its Grants.

```json
{
  "deregistrationRequest": [
    {"cbsdId": "cbsd/..."}
  ]
}
```

The CBSD is deleted and its Grants are removed by cascade.

### Unsupported versions

- `POST /{version}/registration`
- `POST /{version}/heartbeat`

For versions other than `v1.2`, these return code `100` per item.

## Control Plane–SAS

Administrative operations use the `/admin` prefix.

### Registration and access control

| Method and endpoint | Main body | Effect |
|---|---|---|
| `POST /admin/reset` | empty | Deletes and recreates the database |
| `POST /admin/injectdata/fcc_id` | `{fccId, fccMaxEirp}` | Creates or updates an FCC ID |
| `POST /admin/injectdata/user_id` | `{userId}` | Registers an operator |
| `POST /admin/injectdata/conditional_registration` | `{registrationData: [...]}` | Upserts conditionals by FCC ID and serial |
| `POST /admin/injectdata/cpi_user` | `{cpiId, cpiName, cpiPublicKey}` | Creates or updates a CPI |
| `POST /admin/injectdata/blacklist_fcc_id` | `{fccId}` | Blocks all CBSDs for that FCC ID |

Blacklisting does not mark Grants as terminated immediately. Registration,
Spectrum Inquiry, Grant, and Heartbeat then return code `101`; on Heartbeat,
`transmitExpireTime` is returned in the past.

### PAL authorizations and revocation

`POST /admin/injectdata/pal_database_record`

Accepts one PAL object or a list. The record requires `palId` and a frequency
range in `channelAssignment.primaryAssignment` or `palBlock`.

Creation or renewal:

```json
{
  "palId": "pal/123",
  "userId": "operator_a",
  "licenseStatus": "VALID",
  "license": {
    "licenseExpiration": "2027-07-17T19:00:00Z"
  },
  "channelAssignment": {
    "primaryAssignment": {
      "lowFrequency": 3700000000,
      "highFrequency": 3710000000
    }
  }
}
```

Revocation:

```json
{
  "palId": "pal/123",
  "userId": "operator_a",
  "licenseStatus": "REVOKED",
  "license": {
    "licenseExpiration": "2026-07-17T19:00:00Z"
  },
  "channelAssignment": {
    "primaryAssignment": {
      "lowFrequency": 3700000000,
      "highFrequency": 3710000000
    }
  }
}
```

When the same `palId` is resent as `REVOKED`, `EXPIRED`, or any status other
than `VALID`, the SAS:

1. updates the PAL and its `AuthorizationSource`;
2. terminates only Grants derived from that `palId`;
3. sets `authorized=false` and records the termination reason;
4. returns code `500` on the next Heartbeat.

This revokes an authorization, not a `cbsdId` directly. If each device
receives its own PAL, revocation can act as per-device control. There is
currently no administrative endpoint to revoke a CBSD or Grant by identifier
alone.

### Regulatory entities and databases

| Method and endpoint | Purpose |
|---|---|
| `POST /admin/injectdata/fss` | Persist an FSS station |
| `POST /admin/injectdata/wisp` | Persist a WISP protection |
| `POST /admin/injectdata/zone` | Persist a PPA/zone and return the rewritten ID |
| `POST /admin/injectdata/exclusion_zone` | Persist GeoJSON and protected ranges |
| `POST /admin/injectdata/peer_sas` | Register a peer SAS by fingerprint and URL |
| `POST /admin/injectdata/esc_sensor` | Create or update an ESC sensor |
| `POST /admin/injectdata/database_url` | Register an external source for CPAS |

`database_url` types processed by CPAS are `PAL`, `CPI`, `EXCLUSION_ZONE`,
`SCHEDULED_DPA`, `FSS`, and `GWBL`.

### Measurement, protection, and DPA

| Method and endpoint | Effect |
|---|---|
| `POST /admin/trigger/meas_report_in_registration_response` | Request a measurement report in Registration |
| `POST /admin/trigger/meas_report_in_heartbeat_response` | Request a measurement report in Heartbeat |
| `POST /admin/trigger/enable_ntia_15_517` | Enable and persist NTIA zones for the active profile |
| `POST /admin/trigger/dpa_activation` | Persist a DPA activation |
| `POST /admin/trigger/bulk_dpa_activation` | With `activate=false`, clears activations |
| `POST /admin/trigger/load_dpas` | Stub: currently performs no action |

`bulk_dpa_activation` with `activate=true` still does not create activations.

### PPA, FAD, and daily activities

| Method and endpoint | Response / effect |
|---|---|
| `POST /admin/trigger/create_ppa` | Creates a PPA for known `palIds` and returns the ID |
| `POST /admin/get_ppa_status` | Returns `{completed, withError}` |
| `POST /admin/trigger/create_full_activity_dump` | Generates the FAD manifesto and files |
| `POST /admin/trigger/daily_activities_immediately` | Starts CPAS on a background thread |
| `POST /admin/get_daily_activities_status` | Returns `{completed: boolean}` |
| `POST /admin/trigger/enable_scheduled_daily_activities` | Persists the flag; does not start a real scheduler |

### Administrative catch-all

Unknown routes under `/admin/{full_path}` accept `GET`, `POST`, `PUT`,
`DELETE`, and `PATCH` and currently return an empty HTTP 200. That behavior
exists for harness compatibility and must not be treated by the control plane
as confirmation that an operation is implemented.

## SAS–SAS

SAS–SAS routes require a certificate with the SAS role, `clientAuth` EKU, no
zone role, and a fingerprint previously registered through
`/admin/injectdata/peer_sas`. Authorization failures return HTTP 403.

| Method and endpoint | Content |
|---|---|
| `GET /v1.3/dump` | Latest FAD manifesto |
| `GET /v1.3/cbsd/{filename}` | CBSD FAD file |
| `GET /v1.3/zone/{filename}` | Zone FAD file |
| `GET /v1.3/coordination/{filename}` | Coordination FAD file |
| `GET /v1.3/esc_sensor/{path}` | ESC FAD file or sensor lookup |

Missing files normally return HTTP 404. Lookup of an unknown ESC sensor returns
an empty object.

## OpenAPI

FastAPI publishes automatically:

- `GET /openapi.json`;
- `GET /docs`;
- `GET /redoc`;
- `GET /docs/oauth2-redirect`.

OpenAPI documents the structured schemas, but administrative endpoints that
accept free-form JSON should be consulted on this page.
