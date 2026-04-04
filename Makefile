# Repository root — run `make ci` from here (uses nested Makefiles).
SHELL := /bin/bash

.PHONY: ci test

ci:
	$(MAKE) -C astrocyte-services-py ci-checks

test:
	$(MAKE) -C astrocyte-py test
