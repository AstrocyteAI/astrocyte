SHELL := /bin/bash

.PHONY: test

test:
	uv sync --extra dev
	uv run python -m pytest
