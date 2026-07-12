"""Regenerate the checked-in OpenAPI snapshot (openapi.json).

The snapshot is the gateway's HTTP contract artifact: tests/test_openapi_contract.py
fails when the live schema drifts from it, and CI's oasdiff job classifies any
diff as breaking/non-breaking. After an INTENTIONAL API change, refresh it:

    uv run python scripts/generate_openapi.py

and commit the result. Environment-independent by construction: auth mode and
host are pinned before the app is built.
"""

from __future__ import annotations

import json
import os
import pathlib


def generate() -> dict:
    os.environ["ASTROCYTE_AUTH_MODE"] = "dev"
    os.environ["ASTROCYTE_HOST"] = "127.0.0.1"
    os.environ.pop("ASTROCYTE_CONFIG_PATH", None)
    os.environ.pop("ASTROCYTE_RATE_LIMIT_PER_SECOND", None)
    from astrocyte_gateway.app import create_app

    return create_app().openapi()


def main() -> None:
    out = pathlib.Path(__file__).resolve().parent.parent / "openapi.json"
    out.write_text(json.dumps(generate(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
