# Testing

> Draft: overview of the testing strategy and the harness constraints.

## Local tests

The project's unit tests use the standard `unittest` library:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The issuance, renewal, revocation, and eviction cycle can be exercised in
isolation:

```bash
.venv/bin/python -m unittest tests.test_authorization_lifecycle -v
```

## Official WInnForum harness

The CBSD interfaces are verified with the
[Spectrum Access System Test Harness](https://github.com/Wireless-Innovation-Forum/Spectrum-Access-System).
`Dockerfile.harness` clones a pinned revision of the official project during the
build and installs the dependencies in an isolated image.

Download the third-party code and build the image:

```bash
docker build -f Dockerfile.harness -t sas-harness .
```

Run the SIQ, GRA, and HBT suites against a temporary agent instance:

```bash
./run_cbrs_tests.sh
```

The runner generates and extracts temporary certificates, starts the agent with
Docker Compose, runs the suites, and removes the temporary resources when it
finishes.

## Policy for third-party code

- Do not modify the official WInnForum source code or its `requirements.txt`.
- Keep the harness revision pinned so results stay reproducible.
- Make environment adjustments only in `Dockerfile.harness` or in this
  repository's scripts.
- Treat the harness as a black box; changes needed upstream should be proposed
  to the official project.

A local ignored copy exists under `vendor/` in some development environments,
but the automated flow does not depend on it: the clone used by the tests
happens inside the image.

## Python compatibility

The harness runs on Python 3.11 in the container. Its dependency set includes
legacy libraries and native components such as GDAL, Shapely, and pycurl. Python
3.12 or newer is not supported by the validated flow and may cause installation
or runtime failures.

The project installs a compatible NumPy version first and matches the GDAL
Python package to the system library version, without modifying the official
dependency file.

## Topics for future detailing

- matrix of suites and covered requirements;
- regression results per band profile;
- continuous integration and artifact publishing;
- strategy for updating the pinned harness revision;
- separation between protocol conformance and regulatory certification.
