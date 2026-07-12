"""Property-based API conformance: the app must honor its own OpenAPI schema.

Schemathesis generates requests from the schema (now real, thanks to the typed
request models) and asserts the app never 5xxes on schema-valid input.

Bounded to a few examples per endpoint so the suite stays fast; the goal is a
smoke-level conformance gate, not exhaustive fuzzing.

Excluded endpoints:
- /v1/mental-models*, /v1/observations/invalidate — 501 by design when no
  mental_model_store / consolidator is configured (the default test config).
- /v1/ingest/webhook/* — 404s on unknown sources by design and takes a raw
  body, which is not schema-describable.
"""

from __future__ import annotations

import os

import pytest

schemathesis = pytest.importorskip("schemathesis")
from hypothesis import HealthCheck, settings  # noqa: E402

os.environ.setdefault("ASTROCYTE_AUTH_MODE", "dev")
os.environ.pop("ASTROCYTE_CONFIG_PATH", None)
os.environ.pop("ASTROCYTE_RATE_LIMIT_PER_SECOND", None)

from astrocyte_gateway.app import create_app  # noqa: E402

schema = schemathesis.openapi.from_asgi("/openapi.json", create_app())
schema = schema.exclude(path_regex=r"/v1/(mental-models|observations|ingest/webhook)")


# Deliberate unavailability envelopes are conformant: 501 (feature not
# configured) and 503 (dependency not configured, e.g. the storage snapshot
# endpoint without DATABASE_URL). The invariant we gate is "no unhandled
# internal error" — a bare 500 means an exception escaped a handler.
_DELIBERATE_UNAVAILABLE = {501, 503}


@schema.parametrize()
@settings(max_examples=5, deadline=None, suppress_health_check=list(HealthCheck))
def test_api_never_500s_on_schema_valid_input(case) -> None:
    # response_schema_conformance validates real 200 payloads against the
    # documented response models (astrocyte_gateway.response_models) — drift
    # between declared and actual response shapes fails here. checks=[...]
    # REPLACES the default check set, so status-code conformance (our 400s
    # and unavailability envelopes are deliberate) is not enforced.
    from schemathesis.specs.openapi.checks import response_schema_conformance

    response = case.call_and_validate(checks=[response_schema_conformance])
    assert response.status_code < 500 or response.status_code in _DELIBERATE_UNAVAILABLE, (
        f"{case.method} {case.path} returned {response.status_code} "
        f"for schema-valid input: {response.text[:300]}"
    )
