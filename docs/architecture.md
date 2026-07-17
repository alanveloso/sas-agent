# Architecture

> Draft: this document describes the current architecture and the topics to be
> expanded later.

## Overview

- FastAPI exposes the CBSD, administrative, and SAS-to-SAS APIs.
- Uvicorn keeps RSA mTLS listeners on port `9000` and ECDSA on port `9001`.
- SQLAlchemy persists CBSDs, grants, PAL records, and authorization sources in
  SQLite.
- An external control plane publishes allocation decisions through the
  administrative API; the agent validates and enforces those decisions.

## Asynchronous processing

The current implementation does not use Celery or RabbitMQ. Daily
activities/CPAS and FAD synchronization with other SAS peers are started on an
internal thread of the FastAPI process. As a result, there is no independent
worker to run.

A future evolution toward FastAPI + Celery would need to define:

- the broker and result backend;
- queues, retries, and idempotency;
- worker lifecycle and observability;
- task recovery after restarts;
- separation between the HTTP process and the worker processes.

## Authorization lifecycle

### Issuance

- The control plane injects an authorization, currently represented by a valid
  PAL record.
- The agent creates or updates the corresponding `AuthorizationSource`.
- Derived grants are bound to the origin and expiration of the authorization.
- `grantExpireTime` and `transmitExpireTime` never exceed the origin's validity.

### Renewal

- The same authorization identifier is sent with a later expiration.
- New grants and Heartbeats remain bounded by the updated validity.
- The operation must stay idempotent.

### Revocation and eviction

- An invalid, revoked, or expired authorization terminates the derived grants.
- The next Heartbeat response uses WInnForum response code `500`
  (`TERMINATED_GRANT`).
- `transmitExpireTime` is set in the past to stop transmission.
- The authorization update and the grant termination happen in the same
  transaction, following fail-closed behavior.

## Topics for future detailing

- component diagram and trust boundaries;
- mTLS authentication per client type;
- concurrency between the RSA and ECDSA listeners;
- persistence schema and migrations;
- fault tolerance of the control plane and the SAS-to-SAS federation.
