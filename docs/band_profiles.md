# Band profiles

> Draft: initial reference for the available regulatory packages.

## Concept

A `BandProfile` groups, in an immutable way, the rules of a coordinated band:

- lower and upper band edges;
- channel raster width and origin;
- EIRP limits per category and reference bandwidth;
- regulatory rule identifier;
- incumbent, coexistence, and exclusion-zone protections;
- grant duration and Heartbeat intervals.

The profile is selected at process startup through:

1. `BAND_PROFILE_FILE` — path to a JSON file that defines a custom profile;
2. `BAND_PROFILE` — name of a built-in (or previously registered) profile;
3. the default `cbrs_winnforum` when neither variable is set.

The Registration, Spectrum Inquiry, Grant, and Heartbeat services receive the
active profile, preventing the rules of one band from being treated as
universal.

## Custom profile from a JSON file

Users can parameterize the agent without changing Python code by supplying a
JSON configuration file. Ready-made examples mirror the built-in packages:

| File | Purpose |
|---|---|
| `examples/band_profile.cbrs_winnforum.json` | CBRS / FCC Part 96 (3550–3700 MHz) |
| `examples/band_profile.br_anatel_privativa.json` | Anatel SLP private networks (3700–3800 MHz) |
| `examples/band_profile.custom.json` | Lab / experimental starting point |

To verify CBRS via the file path (same parameters as the built-in profile):

```bash
export BAND_PROFILE_FILE=./examples/band_profile.cbrs_winnforum.json
python main.py
# then, with Docker and certificates available:
./run_cbrs_tests.sh
```

Anatel private networks:

```bash
export BAND_PROFILE_FILE=./examples/band_profile.br_anatel_privativa.json
python main.py
```

Or with Docker:

```bash
docker run --rm \
  -e BAND_PROFILE_FILE=/config/band_profile.json \
  -v "$PWD/examples/band_profile.cbrs_winnforum.json:/config/band_profile.json:ro" \
  ...
```

### Parameter reference

All frequencies and bandwidths are integer values in hertz. Durations are
integer values in seconds, power values are floating-point dBm, and protection
flags are JSON booleans (`true` or `false`).

Required parameters:

| Parameter | Meaning |
|---|---|
| `profile_id` | Non-empty unique profile name. The loaded profile is registered under this identifier. |
| `band_low_hz` | Inclusive lower edge of the coordinated band, in Hz. |
| `band_high_hz` | Exclusive upper edge of the coordinated band, in Hz. It must be greater than `band_low_hz`. |
| `channel_width_hz` | Width of the base channel raster, in Hz. It must be greater than zero. |
| `regulatory_rule_id` | Non-empty regulatory rule identifier returned as `ruleApplied` in Spectrum Inquiry responses. |

Optional RF and timing parameters:

| Parameter | Default | Meaning |
|---|---:|---|
| `category_a_max_eirp_dbm` | `30.0` | Maximum Category A EIRP over the reference bandwidth. |
| `category_b_max_eirp_dbm` | `47.0` | Maximum Category B EIRP over the reference bandwidth. |
| `eirp_reference_bandwidth_hz` | `10000000` | Bandwidth over which the category EIRP limits are expressed. The agent converts them to dBm/MHz when evaluating requests. |
| `raster_origin_hz` | `band_low_hz` | Frequency from which channel boundaries are aligned. |
| `fss_exclusion_low_hz` | `band_low_hz` | Lower frequency at which the FSS exclusion policy begins to apply. |
| `fss_protection_km` | `150.0` | FSS protection radius in kilometers. |
| `gaa_policy_id` | `<regulatory_rule_id>_GAA` | Policy identifier used as `AuthorizationSource.source_id` for GAA/coexistence. |
| `grant_duration_sec` | `900` | Nominal maximum lifetime of a grant. An authorization may impose an earlier expiration. |
| `heartbeat_interval_sec` | `60` | Heartbeat interval returned to a CBSD. |
| `transmit_expire_sec` | `60` | Maximum transmission window granted by a successful Heartbeat. |
| `esc_policy` | band edges when `esc` | `{low_hz, high_hz, protection_m}` for peer ESC neighborhood checks. |
| `fss_gwbl_policy` | `fss_exclusion_low_hz`–`band_high_hz` when `fss_gwbl` | Coexistence / GWBL-protected sub-band. |
| `ntia_exclusion_policy` | band edges when `exclusion_zones` | Default frequency envelope for NTIA / federal EXZ contours. |
| `dpa_policy` | band edges when `dpa` | Scheduled / portal DPA frequency envelope. |
| `border_policy` | band edges when `border_protection` | `{low_hz, high_hz, pfd_limit_dbm_m2_mhz, rx_height_m, itm_freq_mhz}`. |

The nested `protection` object controls which regulatory checks are active.
Every omitted flag defaults to `true`:

| Protection flag | Meaning when `true` |
|---|---|
| `pal_gaa` | Enables CBRS PAL/GAA channel classification and priority behavior. |
| `fss_gwbl` | Protects Fixed Satellite Service and Grandfathered Wireless Broadband License records. |
| `exclusion_zones` | Applies configured geographic exclusion zones. |
| `border_protection` | Applies international-border protection rules. |
| `dpa` | Enables Dynamic Protection Area restrictions. |
| `esc` | Enables Environmental Sensing Capability incumbent protection. |
| `quiet_zones` | Applies configured radio quiet-zone restrictions. |

For a non-CBRS profile, set every inapplicable protection explicitly to
`false`; relying on the defaults enables all CBRS-oriented protections.

### Loading and validation

At startup, the JSON root must be an object and all required parameters must be
present. Invalid JSON, a missing file, reversed band edges, non-positive channel
widths, or empty identifiers stop application startup with an error.

When `BAND_PROFILE_FILE` is set, it takes precedence over `BAND_PROFILE`. The
loaded profile is registered under its `profile_id` for the lifetime of the
process.

## `cbrs_winnforum`

Default profile and fallback, compatible with CBRS/WInnForum:

- band: 3550–3700 MHz;
- raster: 10 MHz;
- rule: `FCC_PART_96`;
- EIRP: category A up to 30 dBm/10 MHz and category B up to 47 dBm/10 MHz;
- PAL/GAA, FSS/GWBL, exclusion zones, border, DPA, ESC, and quiet-zone policies
  enabled.

Activation (built-in name or equivalent JSON file):

```bash
export BAND_PROFILE=cbrs_winnforum
# or:
export BAND_PROFILE_FILE=./examples/band_profile.cbrs_winnforum.json
python main.py
```

## `br_anatel_privativa`

Experimental profile for Brazilian private networks under the Serviço Limitado
Privado (SLP):

- band: 3700–3800 MHz;
- base raster: 10 MHz;
- rule: `ANATEL_SLP_ACT_374`;
- EIRP modeled at 47 dBm/100 MHz for categories A and B;
- PAL/GAA, border, DPA, ESC, and quiet zones disabled;
- FSS/GWBL protections and fixed exclusion zones enabled;
- coexistence policy: `ANATEL_SLP_ACT_374_COEXISTENCE`.

Activation (built-in name or equivalent JSON file):

```bash
export BAND_PROFILE=br_anatel_privativa
# or:
export BAND_PROFILE_FILE=./examples/band_profile.br_anatel_privativa.json
python main.py
```

These parameters represent the agent's current modeling and do not constitute a
complete regulatory interpretation or a spectrum authorization.

## Compatibility overrides

`BAND_LOW` and `BAND_HIGH` can change only the edges of the active profile.
These variables do not redefine channelization, power, or protection policies
and should be used with caution.

## Topics for future detailing

- regulatory validation of the power limits;
- support for contiguous blocks larger than the base raster;
- YAML support and multi-profile registries in one file;
- geographic and coexistence rules specific to Anatel;
- homologation criteria for new profiles.
