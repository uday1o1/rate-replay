# Security and privacy

## Claim boundary

This document describes the implemented V1 controls and the threats exercised by local qualifications.
The evidence level is `LOCAL_REPRODUCIBLE`.
Hosted network isolation, managed secret delivery, storage durability, public ACME operation, and hosted restore remain unvalidated.

## Protected assets

The private workflow can hold raw interval uploads, normalized profiles, account credentials, session state, account facts, exact schedules, calculation results, generated reports, audit records, deletion state, backups, and cryptographic key material.
The public demo contains only reviewed simulated aggregate artifacts.

The most sensitive calculation values are exact intervals, daily series, load identifiers, schedules, bill totals, object keys, account identities, and credentials.
They are prohibited from public reports, telemetry labels, and deletion-ledger access-audit payloads.

## Trust boundaries

The browser and Caddy TLS endpoint form the external request boundary.
FastAPI, workers, PostgreSQL, metrics, and object services communicate on internal networks in the reference topology.
The primary object store, backup store, and deletion ledger use separate credentials or keyrings.
Restore runs in quarantine without API, web, or proxy attachment.

The public demo is a separate static trust domain.
It makes only same-origin `GET` requests for build-locked content-addressed files and has no route to private API mutation.

## Threats and controls

| Threat                                  | Primary controls                                                                                | Local verification                             |
| --------------------------------------- | ----------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| Credential guessing                     | Argon2id, fixed password bounds, rate limits, generic failures                                  | Authentication and abuse tests                 |
| Session theft or cross-site mutation    | Secure host-only cookie, `HttpOnly`, `SameSite=Strict`, CSRF token, allowed-origin checks       | API and browser tests                          |
| Cross-account object access             | Owner-scoped resource lookup, non-disclosing `404`, generation authorization                    | Authorization matrix and API tests             |
| Request replay or duplicate publication | Idempotency key plus canonical request hash, semantic identity, unique result claim             | Persistence and integration tests              |
| Malicious XML or oversized input        | Defused streaming parse, entity rejection, relationship validation, size and count bounds       | Parser security and property tests             |
| Tariff-source substitution              | Exact source hashes, effective ranges, component coverage, compiler content hash                | Source locks and admission checks              |
| Incorrect or unverified schedule        | Reference validation, exact lowering, independent replay verifier, fail-closed publication      | Optimizer, oracle, and seeded-defect tests     |
| Raw-data persistence beyond need        | Immediate post-normalization deletion, 24-hour maximum, retention worker                        | Retention and deletion tests                   |
| Deletion lost during restore            | External preparation, database fence, encrypted suppressive ledger, quarantine exposure gate    | Crash injection and restore drill              |
| Backup disclosure                       | Separate credentials, AES-256-GCM object encryption, content-addressed manifest, 30-day maximum | Backup integration and restore qualification   |
| Ledger reading or tampering             | AES-256-GCM records, global chain, signed genesis and head, access audit, versioned keyrings    | Tamper, migration, rotation, and restore tests |
| Secret or private value in telemetry    | Fixed event schemas, allowlisted labels, redaction, no free-form exception payloads             | Telemetry leakage tests and secret scan        |
| Dependency or image vulnerability       | Exact locks, hashed audit export, pnpm audit, pinned Trivy scan, no critical ignores            | Dependency and release qualifications          |

## Authentication and request handling

Usernames are ASCII-lowercase identifiers matching `[a-z0-9_]{3,64}`.
Passwords contain 12 through 128 UTF-8 characters and are never normalized or truncated.
Argon2id uses version 19, time cost 3, memory cost 65,536 KiB, parallelism 4, a random 16-byte salt, and a 32-byte hash.

V1 stores no email address and provides no password recovery.
The interface warns that a lost password makes the local private account unrecoverable.

Every state-changing request requires a valid application session, owner authorization, allowed origin, and CSRF token.
Application problems use a versioned schema and omit internal exception text.

The API enforces one-minute route budgets before executing a handler.
Limiter identity is HMAC-digested, expired state is discarded, and excess distinct identities enter a shared bounded overflow budget.
Forwarding headers are ignored unless the immediate peer is inside an exact trusted proxy CIDR.

## Input and calculation safety

The ESPI parser rejects external entities, entity expansion, broken relationships, unsupported commodities, invalid ReadingType semantics, nonintegral watt-hours, and resource-limit violations.
The CSV adapter accepts only the reviewed provider structure, units, timezone contract, and row bounds.
Unknown formats fail before persistence becomes a confirmed profile.

Tariff definitions are declarative data, not executable source.
The compiler accepts only a closed intermediate-representation operator set and proves numeric bounds.
Comparison and optimization fail closed on missing or unknown inputs.

## Storage and cryptography

PostgreSQL stores application state and lifecycle fences.
Object payloads are content-addressed and encrypted before reaching the object backend when the encrypted adapter is configured.
Runtime secrets enter through mounted files and are excluded from images, committed environment files, command lines, and logs.

Backups use PostgreSQL custom-format dumps and an encrypted object manifest in a separately credentialed store.
Backup encryption and object integrity are verified before a backup can support restore.
The local development data path can be explicitly unencrypted and makes no at-rest encryption claim.

The deletion ledger stores encrypted globally chained records with authenticated clear headers.
HKDF-SHA256 derives distinct encryption, receipt, genesis, head, and active-marker subkeys from exact 32-byte master keys.
Ledger and restore-suppression keyrings are separate and versioned.
Historical keys remain available while a retained backup or unresolved preparation can reference them.

## Deletion and restore privacy

Confirmed raw uploads enter deletion as soon as normalization no longer needs them and have a fixed 24-hour maximum lifetime.
Account, import, and profile deletion fence new work, cancel or subsume scoped jobs, remove database and object state, and preserve only a session-independent receipt and protected control evidence.

Backups may retain encrypted deleted data until their fixed 30-day expiry.
They are not selectively rewritten after each deletion.
The separately protected ledger prevents retained deleted data from becoming exposed after restore.

Every restore uses a new database and object namespace.
A missing or unverifiable ledger, missing historical key, unresolved preparation, tampered artifact, or changed restore instance blocks exposure.

## Public export and demo privacy

The public demo starts from a simulated NREL-derived profile and fixed account facts.
It contains no private bill, utility identifier, address, credential, or customer data.

The redacted report allowlist permits aggregate period, energy, component, cost, provenance, solver, verifier, hash, and limitation fields.
It prohibits exact schedules, interval timestamps, daily series, physical asset keys, occurrence identifiers, object keys, and source identifiers.
The example report in `docs/results` is generated from the same tested payload as the immutable demo object.

## Telemetry and access audit

Structured logs and traces use fixed event names and allowlisted scalar fields.
Metrics labels are bounded and do not contain account, interval, bill, schedule, object, credential, or free-form error values.
The release qualification searches collected telemetry for generated credentials and private inputs.

Every successful deletion-ledger read or mutation first appends an encrypted access-audit record under the same lock.
An audit record contains only fixed actor and operation values, a random operation identity, time, and chain position.
Failure to persist the audit blocks the requested ledger access.

## Residual risks and withheld claims

The filesystem deletion-ledger adapter cannot detect an attacker who atomically replays the entire stream, genesis, head, and active marker together.
A hosted adapter requires conditional append, object lock, or an external witness before supporting a hosted rollback-resistance claim.

The reference limiter is process-local and supports one API process.
A multi-process or multi-host service requires a separately qualified shared limiter.

The current application has no password recovery, utility authorization flow, solar or battery model, device control, or multi-utility tariff set.
These omissions reduce V1 attack surface and remain outside the current claim.

See [architecture/authentication-and-deployment.md](architecture/authentication-and-deployment.md), [architecture/deletion-contract.md](architecture/deletion-contract.md), and [operations/backup-restore.md](operations/backup-restore.md) for the detailed contracts.
