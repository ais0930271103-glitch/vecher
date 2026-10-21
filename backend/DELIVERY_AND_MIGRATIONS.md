# Schema migrations and delivery outbox

## Upgrading the schema

Run from `backend`, with the same database URL and `VECHER_DATA_KEYS` as the API:

```sh
python -m app.migrate check
python -m app.migrate upgrade
```

`check` only reads the schema version. A ready database prints
`SCHEMA_HEAD_OK=0002_outbox`. `upgrade` prints `MIGRATION_OK=0002_outbox` after a
successful transaction. On staging/prod, back up consistently and stop API writes
before upgrading; the staging deployment script provides that sequence.

The first revision freezes the 38-table schema of the working 0.2.1 release in
`alembic/legacy_schema.json`. It does **not** import current model metadata. On a
new database it creates that schema. On an existing database it validates the
known columns, primary keys, foreign keys and unique constraints before adoption.
An incompatible or partial schema stops with `LEGACY_SCHEMA_INCOMPATIBLE`; it is
never blindly stamped or repaired. The known old nullable `wish_cards.level`
column is accepted only if it contains no NULL values. Existing rows and keys are
preserved. Old plaintext in encrypted model columns is explicitly encrypted in
this migration, and existing ciphertext is checked against the configured keys.

The second revision only adds `delivery_outbox` and `delivery_receipts`. PostgreSQL
DDL and the revision marker commit together. SQLite migrations explicitly begin
a transaction before DDL to preserve the same rollback behavior. A PostgreSQL
advisory transaction lock serializes concurrent migration commands. Destructive
downgrades are disabled; they require an explicit recovery plan and verified
backup rather than silently deleting queued work.

Staging/prod API startup only checks the migration head and encryption-key
availability. It never runs `ALTER TABLE`, silently adopts a legacy DB, or
backfills data. Development can initialize a fresh database through the same
migrations. `Base.metadata.create_all` remains available to isolated unit tests,
but an unversioned database containing the new outbox tables is deliberately
rejected by the real migration command.

## Delivery contract

`notify` and `enqueue_delivery` stage an intent in the caller's database
transaction. They never commit, contact Firebase, or start a detached task. The
caller must commit both its business change and the intent; rollback discards
both. Realtime invalidations use the same queue and contain only a version, event
type, event UUID and allowed scopes. Business data is still fetched through the
authenticated REST API.

The complete pending payload is encrypted with the existing DATA_KEYS. Workers
claim work using a DB lease and compare-and-swap updates. PostgreSQL additionally
uses `FOR UPDATE SKIP LOCKED`; SQLite uses the conditional update. A crashed or
cancelled worker leaves its lease to expire, allowing another worker to continue.
Per-device SHA-256 token fingerprints record FCM acknowledgements, so a failure
on one phone does not resend to phones already acknowledged by FCM.

Delivery is **at least once**, not exactly once. A process can crash between a
provider accepting a message and committing its receipt. That one ambiguity can
still duplicate a push; stable event IDs and receipts reduce the duplicate
window. An FCM acknowledgement means provider acceptance, not that the user saw
the notification. Realtime clients must refetch on reconnect because the queue
also cannot guarantee that a disconnected device saw a publication.

The default worker uses a 90-second lease, at most 8 attempts, exponential delay
with jitter, and a 24-hour push deadline. Realtime invalidations expire after
5 minutes; ringing-call pushes use the call's own expiry. Firebase requests are
bounded, OAuth refresh makes one bounded network attempt, and provider retry
hints are respected. Android FCM retention is limited to the remaining job
lifetime, instead of Firebase's default retention period.

Immediately before dispatch the worker rechecks account existence, current pair
membership, current token ownership, pause and notification preferences. Private
push mode replaces title and body at dispatch even when it was enabled after
enqueue. An ended or rejected call cannot produce a new ringing push from the
queue. Pauses and push preferences do not suppress realtime state invalidations.
Pair/account deletion removes queued and leased work and its receipts. Timed
notifications skipped while paused/disabled retain a key-only cancelled marker,
so resuming notifications does not replay the scheduler's past look-back window.

## Retention and operations

Terminal jobs erase their payload but retain identifiers, dedupe keys, timings,
attempt count and a safe reason code. Receipts retain only token hashes and
acceptance status. Those rows are removed with their account/pair. Retaining
terminal keys is intentional: deleting them would let periodic jobs enqueue the
same notification again. If metadata volume becomes significant, add a separate
compaction/archive migration that preserves dedupe semantics; do not periodically
delete completed rows without such a design.

The worker logs safe error codes only. It never logs push bodies, FCM tokens,
provider response bodies or serialized SQL exceptions. The old in-memory
`push.outbox` capture is dev-only and records committed intents, not actual
delivery. Aggregate queue health can be inspected without viewing payloads:

```sql
SELECT kind, state, count(*)
FROM delivery_outbox
GROUP BY kind, state;
```

The meaningful durability tests run on SQLite by default. To exercise the same
suite on a disposable PostgreSQL database, set `VECHER_OUTBOX_TEST_DATABASE_URL`.
Each test creates and drops only its own random schema:

```sh
pytest tests/test_outbox.py tests/test_migrations.py -q
```

These tests cover rollback, encrypted storage, concurrent enqueue/claim races,
crash reclamation, device acknowledgements, token revocation, account/pair/privacy
changes, stale calls, bounded retry/expiry, old-schema adoption, schema-drift
refusal and rollback after an injected failure in a later revision.
