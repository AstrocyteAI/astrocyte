# Repository root — run `make ci` from here (uses nested Makefiles).
SHELL := /bin/bash

.PHONY: ci test

ci:
	$(MAKE) -C astrocytes-services-py ci-checks

test:
	$(MAKE) -C astrocytes-py test
