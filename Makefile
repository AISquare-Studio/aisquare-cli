.PHONY: install test lint typecheck lockcheck fmt check demo

# Every tool runs through the project venv's interpreter when .venv exists —
# a bare tool name resolves through PATH and can land in a sibling env whose
# site-packages shadow this tree (src layout), silently validating a stale
# install. Fallback to PATH's python only when there is no .venv (e.g. CI,
# which installs into its runner env and never had two candidates).
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python)

## install: editable install with dev tools (into .venv when present), ruff and
## mypy at the versions CI pins (ci/constraints.txt)
install:
	$(PYTHON) -m pip install -c ci/constraints.txt -e ".[dev]"

## test: run the test suite, one pytest-xdist worker per core
test:
	$(PYTHON) -m pytest -n auto

## lint: static checks without modifying files (matches CI: format + lint)
lint:
	$(PYTHON) -m ruff format --check src tests
	$(PYTHON) -m ruff check src tests

## typecheck: run mypy over src and tests
typecheck:
	$(PYTHON) -m mypy

## lockcheck: uv.lock still matches pyproject.toml (`uv lock --check`); skipped,
## and said so, where uv is not on PATH — CI's lint job always runs it (#278)
lockcheck:
	@if command -v uv >/dev/null 2>&1; then uv lock --check; \
	else echo "lockcheck: uv not on PATH, skipped (CI runs it)"; fi

## fmt: auto-format and auto-fix lint findings
fmt:
	$(PYTHON) -m ruff format src tests
	$(PYTHON) -m ruff check --fix src tests

## check: everything CI would run
check: lint typecheck lockcheck test

## demo: render docs/demo.tape into out/ (demo.gif, demo.txt, welcome.png); needs docker
demo:
	sh docs/demo/render.sh
