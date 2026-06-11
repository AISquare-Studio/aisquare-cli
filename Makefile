.PHONY: install test lint typecheck fmt check

## install: editable install with dev tools (run inside a virtualenv)
install:
	pip install -e ".[dev]"

## test: run the test suite
test:
	pytest

## lint: static checks without modifying files
lint:
	ruff check src tests

## typecheck: run mypy over src and tests
typecheck:
	mypy

## fmt: auto-format and auto-fix lint findings
fmt:
	ruff format src tests
	ruff check --fix src tests

## check: everything CI would run
check: lint typecheck test
