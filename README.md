# `sas-agent` — Spectrum Enforcement Edge

`sas-agent` is a WInnForum-aligned Spectrum Access System execution agent. It
implements the RF-facing enforcement plane for CBSDs while delegating spectrum
ownership and allocation decisions to an external **control plane**.

That control plane may be a spectrum marketplace, a research orchestrator, a
regulatory simulator, an operator OSS/BSS, or any other policy system that can
inject allocation state through the administrative API. The agent itself does
not own policy; it executes it.

The separation is intentional:

- **Control plane (external):** decides who may use which channel and for how
  long, acting as the policy authority for the deployment.
- **`sas-agent` (execution plane):** persists those decisions, evaluates them
  against CBRS rules, and enforces the resulting state through the CBSD
  Registration, Spectrum Inquiry, Grant, and Heartbeat protocols.
- **CBSD (radio edge):** requests access and obeys the operational parameters
  returned by the agent.

In this model, the agent is an RF enforcement actuator: an external system
injects allocation state and the agent translates it into protocol-level radio
decisions.

```text
┌──────────────────────────┐
│ External control plane   │
│ (marketplace, OSS, ...)  │
└────────────┬─────────────┘
             │ POST /admin/injectdata/pal_database_record
             │ administrator mTLS
             ▼
┌──────────────────────────┐
│ sas-agent                │
│ validation + enforcement │
└────────────┬─────────────┘
             │ WInnForum v1.2 / mTLS
             ▼
┌──────────────────────────┐
│ CBSDs                    │
│ physical RF transmitters │
└──────────────────────────┘
```

> `sas-agent` is a research and engineering reference implementation, not a
> certified commercial SAS or a substitute for authorization by a national
> regulator.

## Capabilities

### Native CBSD interface

The `/v1.2` interface implements the WInnForum CBSD lifecycle, including:

- Registration
- Spectrum Inquiry
- Grant
- Heartbeat
- Relinquishment
- Deregistration

The service exposes two mutually authenticated TLS listeners:

| Listener | Use |
|---|---|
| `https://0.0.0.0:9000` | RSA CBSD, administrator, and SAS-to-SAS traffic |
| `https://0.0.0.0:9001` | ECDSA SAS-to-SAS traffic |

### General Authorized Access (GAA)

GAA access is implemented and operational. The agent discovers available
10 MHz channels, applies incumbent and protected-zone constraints, and issues
and maintains GAA grants through the normal CBSD lifecycle.

Although research integrations often focus on dynamically controlled PAL
rights, GAA remains essential: it provides unlicensed-by-rule access,
coexistence behavior, and a baseline against which controlled PAL allocations
can be compared.

### Dynamic PAL control

The WInnForum PAL injection route is used as a persistent control bus:

```text
POST /admin/injectdata/pal_database_record
```

An injected record is stored in the `PalRecord` table. Reusing a `palId`
updates the existing row, allowing any authorized control-plane system to
publish or renew an allocation without direct database access. PAL ownership,
frequency range, status, and expiration then participate in Spectrum Inquiry
and Grant decisions.

### SAS-to-SAS support

The inherited federation path remains available through the `/v1.3`
SAS-to-SAS interface. It includes peer synchronization, Full Activity Dump
(FAD), mTLS authorization, and periodic CPAS-related processing.

This is retained for standards compatibility and legacy federation. When
allocation policy is driven by an external control plane, direct SAS-to-SAS
synchronization is typically not the primary collision-coordination path;
global policy belongs to the control plane.

## Dynamic configuration

| Variable | Default | Purpose |
|---|---:|---|
| `BAND_LOW` | `3550000000` | Lower Spectrum Inquiry boundary, in Hz |
| `BAND_HIGH` | `3700000000` | Upper Spectrum Inquiry boundary, in Hz |
| `SAS_DATABASE_PATH` | `/data/sas_mvp.db` in Docker | SQLite persistence path |
| `CONTROL_PLANE_API_KEY` | unset | Optional credential for an external gateway or service mesh |

`CONTROL_PLANE_API_KEY` is an optional deployment hint for ingress/gateway
protection and is not currently evaluated by the FastAPI application.
Administrative routes are protected by mTLS; deployments that require an API
key must validate it at an ingress proxy, API gateway, or service mesh. Do not
expose `/admin` directly to an untrusted network.

`BAND_LOW` and `BAND_HIGH` are read when the process starts. A dedicated agent
can therefore advertise a different inquiry range, for example 3.7–3.8 GHz,
by changing only its container environment:

```yaml
services:
  sas-agent-3700:
    build:
      context: .
      dockerfile: sas-agent/Dockerfile
    environment:
      BAND_LOW: "3700000000"
      BAND_HIGH: "3800000000"
      SAS_DATABASE_PATH: /data/sas_mvp.db
      CONTROL_PLANE_API_KEY: "${CONTROL_PLANE_API_KEY:-}"
    ports:
      - "9100:9000"
      - "9101:9001"
    volumes:
      - sas-agent-3700-data:/data
      - /path/to/winnforum-certs:/app/src/harness/certs:ro

volumes:
  sas-agent-3700-data:
```

Build and run directly from the monorepo root:

```bash
docker build -f sas-agent/Dockerfile -t sas-agent .

docker run --rm \
  --name sas-agent-3700 \
  -p 9100:9000 \
  -p 9101:9001 \
  -e BAND_LOW=3700000000 \
  -e BAND_HIGH=3800000000 \
  -e SAS_DATABASE_PATH=/data/sas_mvp.db \
  -v sas-agent-3700-data:/data \
  -v "/path/to/winnforum-certs:/app/src/harness/certs:ro" \
  sas-agent
```

The certificate directory must contain `server.cert`, `server.key`, `ca.cert`,
and, for the ECDSA listener, `server-ecc.cert` and `server-ecc.key`.

> Changing the inquiry boundaries does not by itself redefine all CBRS-specific
> federal protection rules. Operation outside 3550–3700 MHz is experimental
> and requires an audit of band-specific policy and propagation logic.

## Control plane → `sas-agent` interface

### Endpoint

| Property | Value |
|---|---|
| Method | `POST` |
| Path | `/admin/injectdata/pal_database_record` |
| Transport security | Administrator/operator mTLS |
| Content type | `application/json` |
| Success response | HTTP `200`, empty JSON response |
| Idempotency key | `palId` |

The endpoint accepts one PAL object or an array of PAL objects. The following
payload illustrates the WInnForum PAL database record shape:

```json
{
  "palId": "pal/07-2026/20063/1",
  "licensee": "CBSD Operator No. 1",
  "userId": "operator_a",
  "registrationInformation": {
    "callSign": "CALLSIGN-01",
    "frn": "0000000001",
    "licenseConditions": "Control-plane-managed PAL allocation",
    "registrationDate": "2026-07-16T12:00:00Z"
  },
  "license": {
    "licenseId": "license-001",
    "licenseAreaIdentifier": "20063",
    "licenseAreaExtent": "zone/county/census/2017/20063",
    "licenseDate": "2026-07-16T12:00:00Z",
    "licenseExpiration": "2027-07-16T12:00:00Z",
    "licenseFrequencyChannelId": "1"
  },
  "licenseStatus": "VALID",
  "channelAssignment": {
    "primaryAssignment": {
      "lowFrequency": 3570000000,
      "highFrequency": 3580000000
    }
  }
}
```

Example request:

```bash
curl --cert admin.cert --key admin.key --cacert ca.cert \
  -H "Content-Type: application/json" \
  -X POST \
  "https://sas-agent:9000/admin/injectdata/pal_database_record" \
  --data @pal-record.json
```

### Lifecycle semantics

**Activation / issuance**

Send a new `palId`, `licenseStatus: "VALID"`, a future
`license.licenseExpiration`, and a valid primary frequency assignment. The
record becomes available to PAL/PPA evaluation.

**Renewal**

Send the same `palId` with a later expiration. The operation replaces the
persisted PAL fields. Newly issued PAL grants are bounded so that
`grantExpireTime` does not exceed `licenseExpiration`.

**Revocation / eviction**

The control-plane contract represents revocation by sending the same `palId`
with `licenseStatus` other than `VALID` and/or an expiration in the past. The
agent mirrors each PAL into a generic `AuthorizationSource`, terminates every
Grant derived from that source in the same transaction, and fail-closes the
next Heartbeat with WInnForum response code `500` (`TERMINATED_GRANT`) and a
`transmitExpireTime` in the past. Successful Heartbeat renewals never extend
`transmitExpireTime` or `grantExpireTime` beyond `authorization_expires_at`.

## Executando os Testes de Conformidade (WInnForum)

Protocol behavior is exercised with the
[WInnForum Spectrum Access System Test Harness](https://github.com/Wireless-Innovation-Forum/Spectrum-Access-System)
and the WINNF-TS-0061 test suites.

| Suite | Recorded result | Coverage |
|---|---:|---|
| Spectrum Inquiry (`SIQ`) | 12/12 passing | Channel discovery, PAL/GAA availability, and error handling |
| Grant (`GRA`) | 16/16 passing | Grant authorization, channel rules, conflicts, and expiration bounds |
| Heartbeat (`HBT`) | 12/12 passing | Transmit windows, renewals, termination / suspension codes |

These counts are a recorded regression baseline, not a certification claim.
Run the suites again after changes to infrastructure, policy, persistence, or
frequency configuration.

Local authorization-lifecycle checks (no Docker harness required):

```bash
.venv/bin/python -m unittest tests.test_authorization_lifecycle -v
```

O test harness oficial não é armazenado nem baixado para a árvore deste
repositório. O `Dockerfile.harness` clona uma revisão validada do projeto
oficial diretamente durante o build e mantém todo o código de terceiros dentro
da imagem `sas-harness`. Trate essa imagem como uma caixa preta imutável:
**nunca altere o código-fonte original dos testes do WInnForum**.

A única dependência da máquina host é o **Docker**. Não é necessário instalar
Git, Python nem as bibliotecas legadas do harness localmente.

Execute as suítes SIQ/GRA/HBT contra o agente local:

```bash
./run_cbrs_tests.sh
```

O runner, sem intervenção manual:

- constrói a imagem `sas-harness` (Python 3.11 + dependências nativas do
  harness, com o `GDAL` casado à versão do sistema para evitar o pin
  incompatível — o `requirements.txt` oficial nunca é editado);
- clona o harness somente dentro da imagem e gera nela os certificados mTLS;
- extrai apenas os certificados para um diretório temporário fora do
  repositório, removido ao final;
- sobe o `sas-agent` via Docker Compose (`network_mode: host`);
- roda `WINNF_FT_S_SIQ_testcase`, `WINNF_FT_S_GRA_testcase` e
  `WINNF_FT_S_HBT_testcase` dentro do container do harness, com `sas.cfg`
  apontando para `localhost:9000/9001`;
- restaura a configuração original e derruba os contêineres ao final.

> **ALERTA: o test harness oficial do WInnForum recomenda Python 3.11 e declara
> não ser testado no Python 3.12 ou superior. Por isso ele é executado no
> container `sas-harness` (3.11), e não no Python do host. Bibliotecas legadas e
> com versões fixas (GDAL, Shapely, pycurl, etc.) podem falhar fora dessa
> versão; nunca corrija esses problemas editando o código oficial dos testes.**

## Implementation notes

- FastAPI provides the HTTP interfaces.
- SQLAlchemy and SQLite persist CBSD, grant, PAL, authorization sources, peer
  SAS, and injected regulatory state.
- Grants bind to a generic authorization origin (`authorization_source_type` /
  `authorization_source_id` / `authorization_expires_at`) so control-plane
  revocations cascade into Heartbeat termination.
- mTLS authenticates CBSDs, administrators, and peer SAS nodes.
- The Docker image exposes ports `9000` and `9001` and persists its database
  under `/data`.
- Simplified geometry and propagation behavior make this repository suitable
  for research, protocol validation, and controlled experiments; it is not a
  high-fidelity RF planning engine.

## License

`sas-agent` is licensed under the Apache License, Version 2.0. See
[LICENSE](LICENSE). The WInnForum test harness and specifications are separate
works governed by their respective licenses and terms.
