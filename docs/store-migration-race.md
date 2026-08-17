# The migration race on a fresh store

**Status: reproduced and characterised, NOT fixed.** This page exists so the fix
starts from evidence instead of from a fresh hour of guessing. Everything below
was measured; where a hypothesis was falsified it is recorded as falsified,
because the falsified ones are the expensive part.

## What happens

Several sessions opening a **fresh** store at the same instant — which is the
shape of a morning where a crew launches together — can raise a **non-transient**
`sqlite3.OperationalError` out of `core.store._migrate`:

```
OperationalError('duplicate column name: account')
OperationalError('duplicate column name: model')
```

This is not a lock timeout. `open_store` promises a bounded wait and then a
clean `database is locked`; this is neither, and
`tests/test_team.py::test_concurrent_first_opens_migrate_safely` fails it
correctly via `assert real_errors == []`.

## Reproducing it

Two minutes, and it does not need luck — it needs the box to be **oversubscribed**,
so run it while something else is burning the CPUs:

```bash
# 48 CPU hogs on a 16-core box, then:
AISQUARE_DB_BUSY_MS=50 python - <<'PY'
import os, tempfile, threading
from aisquare.core.store import store_session
for _ in range(40):                       # ~30-60 races is enough
    os.environ["AISQUARE_HOME"] = tempfile.mkdtemp()
    barrier = threading.Barrier(6)
    def opener():
        barrier.wait()
        try:
            with store_session() as store:
                store.entries("user")
        except Exception as exc:
            print(type(exc).__name__, exc)
    threads = [threading.Thread(target=opener) for _ in range(6)]
    for t in threads: t.start()
    for t in threads: t.join()
PY
```

Observed rate: **57 non-transient failures across 60 races** (55 `account`, 2
`model`); a second run gave 51. Idle, it does not reproduce at all.

## What is actually going wrong

`_migrate` reads `PRAGMA user_version`, picks `_MIGRATIONS[version]`, and applies
it in one `BEGIN IMMEDIATE` transaction that also bumps the version. A loser
whose script fails re-reads the version: if someone else advanced it, that is
victory by other means; otherwise the error is real and it raises.

Instrumenting the loser branch (517 observations over 60 races) shows the
swallow path works — losers that read version 0 and re-read 4, 5 or 6 correctly
conclude they lost a race. The **escapes** are the interesting ones, and reading
the version from an *independent* connection at the moment of failure is what
makes them legible:

| error | migration index run | version, same connection | version, fresh connection | script |
|---|---|---|---|---|
| `duplicate column name: account` | 8 | 8 | 8 | `ALTER TABLE team_session ADD COLUMN account TEXT` |
| `duplicate column name: model` | **9** | **8** | **8** | `ALTER TABLE team_session ADD COLUMN model TEXT; …` |

Two facts, and the second is the one to design against:

1. **A migration's DDL is visible while its version bump is not.** At version 8
   the `account` column already exists, so every subsequent attempt at
   migration 8 fails forever on the same database.
2. **The version goes BACKWARDS.** The second row ran migration index 9, which
   means it read version 9 at the top of the loop — and after the failure both
   its own connection and a brand-new one report 8. A version regression is not
   something an atomic bump-inside-the-transaction can produce on its own.

Together those say the two connections are not looking at the same database
state: one is reading content the other has already superseded.

## Hypotheses, including the falsified ones

**FALSIFIED — "`executescript` breaks the transaction."** Measured directly: a
mid-script failure inside `BEGIN IMMEDIATE; … COMMIT;` leaves the transaction
open, `ROLLBACK` succeeds, and the earlier `ALTER TABLE` **is** rolled back
(`PRAGMA table_info` shows the column gone). A second connection attempting
`BEGIN IMMEDIATE` against a held write lock is blocked with `database is
locked`. The transaction machinery in `_migrate` does what its docstring says.

**FALSIFIED — "take `BEGIN IMMEDIATE` first, then re-read the version under the
lock."** The obvious atomicity fix. It makes things **much worse**: 297 escapes
and new failure kinds (`table entry already exists`, `no such index:
team_task_project_status`). Cause: `sqlite3.executescript()` performs an
implicit `COMMIT` **before** running its script, which releases the lock you
just took. Anyone reaching for this fix should stop here.

**OPEN, and where the evidence points — the journal-mode switch.** `open_store`
runs `PRAGMA journal_mode = WAL` before `_migrate`, in a retry loop, on
connections opened at different moments. A connection that has not observed the
switch reads the main database file rather than the WAL, which would explain
both facts above at once: checkpointed DDL visible in the main file, a version
bump still living in the WAL, and therefore a version that appears to go
backwards between connections. **This has not been proven.** It is the next
thing to test, not a conclusion.

## What must not be done to make it go away

- Do not catch `duplicate column name` and continue. The column existing at the
  wrong version is the symptom of two connections disagreeing about the
  database; swallowing it leaves that disagreement in place for every later
  migration, and the next one to land will not be idempotent.
- Do not relax `assert real_errors == []` in the race test. That assertion is
  what surfaced this, and it is the only thing standing between this defect and
  a silent one.
