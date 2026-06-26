# ADR-0004: LLM-Brain compose network model

Status: Accepted (M5 baseline)
Date: 2026-06-21
Deciders: local operator
Related: ADR-0001 (RAG ingress queue boundary), ADR-0003 (ledger PostgreSQL cutover)

---

## Context

M5 asks for a portable single `docker compose` path for the LLM-Brain runtime:
CouchDB source, ledger PostgreSQL, Neo4j graph, and the Vertex-compatible LLM
wrapper must run from a clean checkout without depending on ad-hoc host wiring.

The repo already has one compose authority, `compose.yaml`, and it uses the
default Compose bridge network with service DNS for owned services. For example,
`ingress-api` reaches NATS through `nats://nats-jetstream:4222`, and the
`llm-brain-graph` profile publishes Neo4j to the host only for operator access
while the container-facing URI is `bolt://llm-brain-neo4j:7687`.

The ambiguous part is the network model for the remaining M5 services:

1. run everything with `network_mode: host`;
2. keep a single compose project and use bridge-network service DNS;
3. split LLM-Brain into several compose projects and bind them through host
   ports.

This decision must preserve the repo boundary from ADR-0001: `neurons` does not
mutate the existing RAGFlow compose stack, its databases, or its volumes as part
of ordinary LLM-Brain work.

## Decision

Use a **bridge-first single compose project** for all `neurons`-owned LLM-Brain
services.

`compose.yaml` remains the portable authority for this repo. M5 additions should
be profile-gated and attached to the default project network unless a service
has a documented reason to be external. Runtime clients inside the compose
project must use service DNS, not host loopback:

| Capability | Container-facing address | Host-facing address |
| --- | --- | --- |
| Neo4j graph | `bolt://llm-brain-neo4j:7687` | `127.0.0.1:${LLM_BRAIN_NEO4J_BOLT_PORT:-17687}` |
| CouchDB source | `http://llm-brain-couchdb:5984` | `127.0.0.1:${LLM_BRAIN_COUCHDB_PORT:-15984}` |
| Ledger PostgreSQL | `postgresql://llm-brain-ledger-postgres:5432/...` | `127.0.0.1:${LLM_BRAIN_LEDGER_POSTGRES_PORT:-15432}` |
| Vertex wrapper | `http://llm-brain-vertex-wrapper:8080/v1` | optional loopback port for smoke only |

Host-published ports are operator and smoke-test conveniences. They must be
bound to `127.0.0.1` unless a later ADR explicitly widens exposure. They are not
the default path between owned services.

RAGFlow remains outside this compose project. Any integration with live RAGFlow
uses explicit adapter configuration, bounded operator approval, and redacted
postchecks; it is not achieved by mounting or mutating RAGFlow volumes.

Secrets stay in environment inputs or env files ignored by git. The checked-in
examples may include only placeholder values. The Vertex wrapper may mount ADC
credentials read-only through an explicit path, but the compose file must not
embed credential material.

## Options Considered

### Option A: `network_mode: host`

Pros:
- Simple connection strings on one Linux host.
- Matches some live one-off commands that address host-local services.

Cons:
- Not portable to Docker Desktop and clean machines.
- Removes Compose DNS isolation and increases port collision risk.
- Makes service dependency and health wiring less explicit.
- Encourages runtime clients to depend on host loopback instead of owned service
  names.

### Option B: single compose project with bridge DNS (selected)

Pros:
- Portable across Linux hosts and Docker Desktop.
- Gives every owned service a stable DNS name.
- Keeps host ports as explicit operator boundaries.
- Matches the current `compose.yaml` direction.
- Lets `docker compose --profile ... config` validate a clean checkout without
  requiring live host services.

Cons:
- Requires service-specific container URLs that differ from host smoke URLs.
- Services that call a host-only dependency still need an explicit, documented
  bridge such as `host.docker.internal`.

### Option C: multiple compose projects bound through host ports

Pros:
- Lets each subsystem evolve independently.
- Can be useful for live migration when one subsystem is already deployed.

Cons:
- Recreates the host-port coupling that M5 is meant to remove.
- Makes clean-checkout bring-up order and health checks less obvious.
- Blurs the owned `neurons` runtime boundary.

## Consequences

### Positive

- A clean checkout has one repo-local compose entry point for LLM-Brain runtime.
- Service-to-service URLs are stable and testable through Compose DNS.
- Existing RAGFlow stack and volumes remain outside ordinary code work.
- Host ports stay loopback-only by default, reducing accidental exposure.

### Negative

- Documentation must distinguish container-facing URLs from host-facing smoke
  URLs.
- The live Ubuntu topology may keep operator-specific env overrides during
  migration; those overrides must not become the portable default.

### Neutral

- ADR-0003's `NEURON_LEDGER_PG_DSN` env flip remains the application cutover
  switch. This ADR only decides how the PostgreSQL service is addressed when the
  service is compose-owned.
- `host.docker.internal` can remain for approved external host dependencies, but
  it is an exception, not the intra-compose default.

## Done Criteria

1. `docker compose --profile llm-brain-graph config` renders without requiring
   host-network mode.
2. M5 CouchDB, ledger PostgreSQL, and Vertex wrapper services are added as
   profile-gated services in `compose.yaml`.
3. Runtime env examples prefer service DNS for container-facing addresses and
   loopback ports only for operator access.
4. Checked-in env examples contain placeholders only and no credential material.
5. Clean-machine smoke proves healthz, tools/list or equivalent capability
   discovery, graph status, and one entity-extraction recall path without
   mutating RAGFlow volumes.

## References

- `compose.yaml`
- `README.md` Compose isolation section
- `docs/runbooks/LLM_BRAIN_CORE_V1_LOCAL_OPS.md`
- ADR-0001: RAGFlow target adapter and RAGFlow compose isolation
- ADR-0003: ledger PostgreSQL cutover and `NEURON_LEDGER_PG_DSN`
