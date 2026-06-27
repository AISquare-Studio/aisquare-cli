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
