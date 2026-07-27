# Session tenant-scope migration contract

This document records what the current Alembic history can prove, the safe
deployment gate, and the remaining limitation. It is intentionally stricter
than the migration comments: a phase is complete only when its database query
and writer-version gate pass.

## Current revision sequence

| Phase | Revision / code evidence | Required proof before continuing |
|---|---|---|
| Expand | `0011_session_tenant_scope_expand` adds nullable `turns.tenant_id`, the composite session unique key/FK, and the historical backfill | Migration is exactly at 0011 and the old schema remains writable |
| Application dual-write | `SessionManager` writes `tenant_id` on every new `TurnRow`; Redis context, lock, and fence keys use `session:v2:*:{tenant}:{session}` | All active application writers are the tenant-aware version |
| Backfill | 0011 performs the first backfill; 0014 repeats it and aborts if any NULL remains | `SELECT count(*) FROM turns WHERE tenant_id IS NULL` returns `0` |
| Read switch | Tenant-aware `SessionManager` reads sessions and turns using both `tenant_id` and `session_id` | Cross-tenant duplicate-session tests pass; no pre-tenant writer remains active |
| Contract | `0014_session_tenant_scope_contract` makes the composite session key physical and `turns.tenant_id` NOT NULL | Old application processes are fully drained before applying 0014 |
| Legacy-writer fence | `0021_turn_tenant_writer_compat` infers an omitted tenant only when one tenant owns the session ID; missing, ambiguous, and mismatched ownership fails closed | Integration test `test_turn_tenant_writer_compat.py` passes on PostgreSQL |
| Remove compatibility | A future revision drops the 0021 trigger after one release with no legacy-writer inference log events | Every supported writer always supplies `tenant_id` |

## Safe deployment procedure for the existing history

1. Back up Postgres and record the current Alembic revision.
2. If the database is before 0011, apply only through 0011.
3. Deploy the tenant-aware application, but do not allow pre-tenant and
   tenant-aware writers to process the same session traffic concurrently.
4. Drain every pre-tenant API/worker replica. Verify the deployment inventory
   and worker heartbeats contain only the tenant-aware release.
5. Run the NULL and ownership checks below. Any row returned blocks cutover.
6. Apply migrations through 0021, then start tenant-aware replicas.
7. Monitor PostgreSQL logs for `legacy turn writer inferred tenant scope`.
   Any event means an unsupported writer is still active and must be removed.

```sql
SELECT count(*) AS tenantless_turns
FROM turns
WHERE tenant_id IS NULL;

SELECT turn_row.turn_id
FROM turns AS turn_row
LEFT JOIN sessions AS session_row
  ON session_row.tenant_id = turn_row.tenant_id
 AND session_row.session_id = turn_row.session_id
WHERE session_row.session_id IS NULL
LIMIT 1;
```

The application must not start schema migration implicitly. Run the dedicated
Alembic migration job first; runtime roles perform read-only schema-contract
verification.

## Explicit limitation

The historical 0011 revision backfills rows that exist when it runs, but it
does not install a database dual-write trigger. Therefore an old process can
insert a tenant-less turn after the 0011 backfill, and a tenant-scoped reader
can temporarily miss it until 0014 repeats the backfill. Revision 0021 is after
the already-shipped 0014 contract and cannot retroactively close that rolling
window without rewriting migration history.

Consequently the present history supports a data-preserving, verifiable
cutover with an old-writer drain gate, not a fully zero-downtime mixed-version
cutover. Rewriting 0011 would make fresh installations appear safer while
leaving already-migrated databases unchanged, so it is forbidden. The 0021
forward migration provides fail-closed one-release compatibility after the
contract; a future migration may remove it only after the log gate remains at
zero.
