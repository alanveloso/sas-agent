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

The profile is selected at process startup through the `BAND_PROFILE` variable.
The Registration, Spectrum Inquiry, Grant, and Heartbeat services receive the
active profile, preventing the rules of one band from being treated as
universal.

## `cbrs_winnforum`

Default profile and fallback, compatible with CBRS/WInnForum:

- band: 3550–3700 MHz;
- raster: 10 MHz;
- rule: `FCC_PART_96`;
- EIRP: category A up to 30 dBm/10 MHz and category B up to 47 dBm/10 MHz;
- PAL/GAA, FSS/GWBL, exclusion zones, border, DPA, ESC, and quiet-zone policies
  enabled.

Activation:

```bash
export BAND_PROFILE=cbrs_winnforum
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

Activation:

```bash
export BAND_PROFILE=br_anatel_privativa
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
- external configuration and profile versioning;
- geographic and coexistence rules specific to Anatel;
- homologation criteria for new profiles.
