# Contributing to aisquare

Thanks for your interest in aisquare! This is an early-stage, open-source
project and contributions are welcome.

## Development setup

Requires Python 3.11+.

```sh
python3 -m venv .venv
source .venv/bin/activate
make install          # editable install + dev tools (ruff, mypy, pytest)
```

> **Do not install the explainability extra into this checkout.**
> `pip install 'aisquare-cli[explainability]'` puts a second `aisquare` package
> ahead of your editable one, and from then on every command — including the
> ones that would have explained it — dies with
> `No module named 'aisquare.cli'`. Install the extra in a separate,
> non-editable environment instead. If you already did: `pip uninstall aisquare`.

## Before you open a PR

Run the full check suite — this is exactly what CI runs:

```sh
make check            # lint + typecheck + tests
```

Individual targets:

| Task | Command |
| --- | --- |
| Format + autofix | `make fmt` |
| Lint | `make lint` |
| Type-check (mypy strict) | `make typecheck` |
| Tests | `make test` |

CI (`.github/workflows/ci.yml`) runs lint, format-check, mypy and pytest on
Python 3.11–3.13, plus a packaging job that builds the wheel and smoke-tests the
`aisquare` / `asq` console scripts. All jobs must pass before a PR can merge.

### Proving a test can fail

Many guards in this suite are written to catch something specific, so the useful
question is not "does it pass" but "can it fail". The usual method is to break
the thing on purpose, watch the test go red, and restore. **That method has a
trap, and it is silent.**

Python caches compiled bytecode — including pytest's rewritten test modules —
and decides the cache is fresh by comparing the source's **modification time in
whole seconds and its size in bytes**. A mutation that changes neither is
invisible: the old bytecode runs and the file on disk is not the code under
test. Measured on this repo:

```sh
# a test asserting "AAA" == "AAA", edited to "AAA" == "BBB" and re-run
# within the same second
1 passed          # the failing assertion never ran
rm -rf __pycache__
1 failed          # same file, correct result
```

Same-size edits are more common than they sound: swapping two names of equal
length, reordering a tuple, changing a digit, transposing two arguments. Both
directions hurt — a restore that does not take leaves the mutation live, and a
mutation that does not take reports a false green, which is the worse one
because you then report a defect that is not there.

Two habits, and they cover different moments:

- **Prevent it.** `find . -name __pycache__ -type d -prune -exec rm -rf {} +`
  between the mutation and the measurement.
- **Detect it.** Assert *which* test fails, not that something failed. A proof
  whose expected outcome is one bit cannot distinguish "the mutation is wrong"
  from "the mutation never ran"; naming the expected failure makes a stale run
  visible, including in a proof you already wrote up.

Mutations to Markdown, JSON or any non-imported file are structurally immune —
there is no bytecode to go stale.

## Implementing a feature (stub → service)

Most commands are still stubs that exit `70`. Each one becomes real by replacing
a single `stub(...)` call in a `services/` module — the CLI wiring and function
signatures already exist. The flow is:

1. **Implement the service** in `src/aisquare/services/<domain>.py`. Services
   return data; they never parse CLI arguments or print. Persisted state goes
   through the `ContextStore` in `src/aisquare/core/store.py`.
2. **Render it** in the matching `src/aisquare/cli/<group>.py` command: parse,
   call the service, print (honouring `--json` via `get_state().json_output`).
   Shared rendering helpers live in `cli/common.py`.
3. **Move the command off the stub skip-list** in `tests/test_stubs.py`
   (`IMPLEMENTED`) and add real tests for the new behaviour.

See the README's [Architecture](README.md#architecture) section for the full
layout and the thin-CLI / service / core split.

## Conventions

- **Type everything.** `mypy` runs in `strict` mode over `src` and `tests`.
- **Tests are isolated.** The `isolated_home` fixture points `AISQUARE_HOME` at
  a temp dir, so tests never touch your real `~/.aisquare`. Never write to the
  real home in a test or example.
- Keep CLI modules thin and services free of CLI concerns.
- New shared plumbing goes in `core/`; new domain shapes go in `models.py`.

## Reporting bugs / proposing features

Open an issue describing what you expected and what happened. For larger
changes, it's worth opening an issue to discuss the approach before writing
code.
