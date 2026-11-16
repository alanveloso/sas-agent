# `sas-agent` — Spectrum Enforcement Edge

`sas-agent` is a Spectrum Access System (SAS) execution agent aligned with the
WInnForum protocols. It exposes mTLS interfaces for the CBSD lifecycle and
enforces allocation decisions received from an external control plane.

> This project is a research and engineering reference implementation. It is
> not a certified commercial SAS and is not a substitute for authorization by
> the relevant national regulator.

## Prerequisites

- Python 3.11;
- `pip` and support for virtual environments (`venv`);
- test mTLS certificates in `../src/harness/certs`;
- Docker Engine with the Docker Compose plugin to run the WInnForum suite;
- RabbitMQ is not required by the current implementation: asynchronous tasks
  run on an internal thread of the FastAPI process.

## Installation

From the root of this repository:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Running

Start the RSA (`9000`) and ECDSA (`9001`) HTTPS/mTLS listeners:

```bash
source .venv/bin/activate
python main.py
```

To parameterize the RF / regulatory package without changing code, pass a JSON
band profile. Built-in equivalents are under `examples/`:

```bash
# CBRS / WInnForum (3550–3700 MHz) — use this to verify CBRS behaviour
export BAND_PROFILE_FILE=./examples/band_profile.cbrs_winnforum.json

# Anatel private networks (3700–3800 MHz)
# export BAND_PROFILE_FILE=./examples/band_profile.br_anatel_privativa.json

python main.py
```

Or select a built-in profile with `BAND_PROFILE=cbrs_winnforum` (default) or
`BAND_PROFILE=br_anatel_privativa`. See [docs/band_profiles.md](docs/band_profiles.md).

The service requires `server.cert`, `server.key`, `server-ecc.cert`,
`server-ecc.key`, and `ca.cert` in `../src/harness/certs`.

There is no separate Celery worker to start. The daily-activities/CPAS worker is
created internally by the service when an activity is triggered through the
administrative API, so `python main.py` starts everything the current
implementation requires.

Alternatively, run the agent in a container:

```bash
export HARNESS_CERTS_DIR=/path/to/winnforum-certs
# Optional; defaults to cbrs_winnforum
export BAND_PROFILE=br_anatel_privativa
docker compose up --build
```

Use separate Compose project names and host ports to run multiple band
instances. Each project receives its own database volume:

```bash
# CBRS instance
BAND_PROFILE=cbrs_winnforum \
HOST_RSA_PORT=9000 HOST_ECC_PORT=9001 \
docker compose -p sas-cbrs up --build -d

# Anatel instance
BAND_PROFILE=br_anatel_privativa \
HOST_RSA_PORT=9100 HOST_ECC_PORT=9101 \
docker compose -p sas-anatel up --build -d
```

To stop it:

```bash
docker compose -p sas-cbrs down
docker compose -p sas-anatel down
```

## Laboratory evaluation

Single entrypoint for host checks, campaign stages, validation of published
results, and regeneration of tables/figures:

```bash
./scripts/evaluate.sh help
./scripts/evaluate.sh validate
./scripts/evaluate.sh tables
./scripts/evaluate.sh figures
```

## WInnForum conformance suite

The official harness is downloaded automatically during the Docker image build,
pinned to a fixed revision. To download and prepare that image:

```bash
docker build -f Dockerfile.harness -t sas-harness .
```

To build the agent, start the containers, and run the Spectrum Inquiry, Grant,
and Heartbeat suites:

```bash
./run_cbrs_tests.sh
```

The runner uses Python 3.11 inside the container. The third-party code must not
be modified, and its legacy dependencies may fail on Python 3.12 or newer.

Local authorization-lifecycle checks:

```bash
.venv/bin/python -m unittest tests.test_authorization_lifecycle -v
```

## Documentation

- [Architecture and authorization lifecycle](docs/architecture.md)
- [API reference: CBSD–SAS, Control Plane–SAS, and SAS–SAS](docs/api.md)
- [Band profiles](docs/band_profiles.md)
- [Testing and third-party code](docs/testing.md)

## License

Distributed under the Apache License 2.0. See [LICENSE](LICENSE). The WInnForum
harness and specifications are separate works, governed by their respective
licenses and terms.
