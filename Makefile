# Repository root — run `make ci` from here (uses nested Makefiles).
SHELL := /bin/bash

.PHONY: ci test codeql

ci:
	$(MAKE) -C astrocyte-services-py ci-checks

test:
	$(MAKE) -C astrocyte-py test

codeql:
	$(MAKE) -C astrocyte-py codeql

# Cut a versioned API-reference docs snapshot for a release (see RELEASING.md).
# Run ON the release commit, BEFORE tagging, so the snapshot is byte-derived
# from the same tree that builds the wheels / image / openapi.json.
.PHONY: docs-version
docs-version:
	@test -n "$(VERSION)" || (echo "usage: make docs-version VERSION=x.y.z" >&2; exit 1)
	cd astrocyte-py && uv run python ../docs/scripts/generate-api-index.py
	python3 docs/scripts/cut-reference-version.py $(VERSION)
	cd astrocyte-py && uv run python ../tooling/check_docs_coverage.py
