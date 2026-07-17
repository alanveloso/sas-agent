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

The service requires `server.cert`, `server.key`, `server-ecc.cert`,
`server-ecc.key`, and `ca.cert` in `../src/harness/certs`.

There is no separate Celery worker to start. The daily-activities/CPAS worker is
created internally by the service when an activity is triggered through the
administrative API, so `python main.py` starts everything the current
implementation requires.

Alternatively, run the agent in a container:

```bash
export HARNESS_CERTS_DIR=/path/to/winnforum-certs
docker compose up --build
```

To stop it:

```bash
docker compose down
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
- [Band profiles](docs/band_profiles.md)
- [Testing and third-party code](docs/testing.md)

## License

Distributed under the Apache License 2.0. See [LICENSE](LICENSE). The WInnForum
harness and specifications are separate works, governed by their respective
licenses and terms.
