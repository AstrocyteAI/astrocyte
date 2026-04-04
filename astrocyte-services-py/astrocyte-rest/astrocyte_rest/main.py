"""CLI entry: `astrocyte-rest` or `python -m astrocyte_rest`."""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    host = os.environ.get("ASTROCYTES_HOST", "127.0.0.1")
    port = int(os.environ.get("ASTROCYTES_PORT", "8080"))

    uvicorn.run(
        "astrocyte_rest.app:create_app",
        host=host,
        port=port,
        factory=True,
    )


if __name__ == "__main__":
    main()
