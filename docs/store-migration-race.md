# The migration race on a fresh store

**Status: found, characterised, fixed.** This page is kept because the route to
the cause ran through two plausible wrong answers, and the wrong answers are the
expensive part to rediscover.

## What happened

Several sessions opening a **fresh** store at the same instant — which is what a
crew launching together onto a new machine looks like — could raise a
**non-transient** `sqlite3.OperationalError` out of `core.store._migrate`:

```
OperationalError('duplicate column name: account')
```

Not a lock timeout. `open_store` promises a bounded wait and then a clean
`database is locked`; this was neither. And the damage was **permanent**: the
column existed while `user_version` still read 8, so every later attempt at
migration 8 failed on that database forever.

## The cause

Time-of-check / time-of-use. `_migrate` read `PRAGMA user_version`, chose
`_MIGRATIONS[version]`, and only then started the transaction. Between the read
and the `BEGIN IMMEDIATE`, another opener could advance the schema — so this one
applied an **old migration to a newer database**.

Instrumenting the loser branch made it legible. Reading the version from an
*independent* connection at the moment of failure:

| error | migration index run | version, same connection | version, fresh connection |
|---|---|---|---|
| `duplicate column name: account` | 8 | 8 | 8 |
| `duplicate column name: model` | **9** | **8** | **8** |

The second row is the one that names the bug: it ran migration index 9, so it
had read 9 — and both connections then agreed the database was at 8. A thread
was acting on a version that was no longer true.

## The fix

Take the write lock first, re-read the version **under** it, then apply. The
window closes because there is no longer a moment between deciding and acting.

`executescript` cannot be used for the transactional part — it issues an
implicit `COMMIT` **before** running its script, which releases a lock taken
beforehand. Statements are split with `sqlite3.complete_statement`, SQLite's own
tokenizer, so a semicolon inside a string or a `CREATE TRIGGER … BEGIN … END`
body does not split a statement. A test compares the schema built by the split
path against the schema `executescript` built, object for object including SQL:
29 objects, identical.

## Hypotheses that were wrong

Both were plausible, both were measured, both were wrong.

**"`executescript` breaks the transaction."** It does not. A mid-script failure
inside `BEGIN IMMEDIATE; … COMMIT;` leaves the transaction open, `ROLLBACK`
succeeds, and the earlier `ALTER TABLE` **is** rolled back. A second connection
attempting `BEGIN IMMEDIATE` against a held write lock is properly blocked. The
transaction machinery was always correct — the *ordering* around it was not.

**"The connections disagree about journal mode."** The evidence fit: DDL visible
while a version bump was not, and a version that appeared to move backwards, is
exactly what a connection reading the main file instead of the WAL would
produce. Measured at the moment of failure, the failing connection and an
independent one both reported `journal_mode = wal` **and** the same
`user_version`. Everyone agreed on the state; the disagreement was about *time*,
not about the file.

## Testing it

Reproducing the original failure needs concurrency **and** luck — measured, it
fires in 0–2 of 15 twelve-way races depending on what else the box is doing. A
test that raced would therefore be the load-sensitive kind this suite has
already had to repair twice: asserting about the machine while appearing to
assert about itself.

So the guard asserts the **invariant** instead, deterministically, using
SQLite's trace callback on a real first open: after every write lock, the
version is re-read before any DDL runs. Pre-fix the order was `READ, BEGIN,
DDL`; post-fix it is `BEGIN, READ, DDL`. Verified red against the pre-fix form
and green after — a guard that has only ever passed proves nothing.

The concurrent-open test that remains is honest about being a smoke test: it
covers that the common path still ends somewhere valid, and does not claim to
guard the fix.

## Reproducing the original, if you ever need to

Needs openers, not unusual load. Twelve is enough, and a fresh `AISQUARE_HOME`
per attempt is essential — the bug is specifically about the FIRST open.

```bash
AISQUARE_DB_BUSY_MS=50 python - <<'PY'
import os, tempfile, threading
from aisquare.core.store import store_session
for _ in range(15):
    os.environ["AISQUARE_HOME"] = tempfile.mkdtemp()
    barrier = threading.Barrier(12)
    def opener():
        barrier.wait()
        try:
            with store_session() as store:
                store.entries("user")
        except Exception as exc:
            print(type(exc).__name__, exc)
    threads = [threading.Thread(target=opener) for _ in range(12)]
    for t in threads: t.start()
    for t in threads: t.join()
PY
```

## What must not be done, if it ever comes back

- Do not catch `duplicate column name` and continue. The column existing at the
  wrong version is a symptom of a thread acting on stale information; swallowing
  it leaves that in place for every later migration.
- Do not relax `assert real_errors == []` in the concurrent-open tests. That
  assertion is what surfaced this, and it is the only thing standing between a
  defect like it and a silent one.
