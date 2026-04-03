"""CLI entry: `astrocytes-server` or `python -m astrocytes_server`."""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    host = os.environ.get("ASTROCYTES_HOST", "127.0.0.1")
    port = int(os.environ.get("ASTROCYTES_PORT", "8080"))

    uvicorn.run(
        "astrocytes_server.app:create_app",
        host=host,
        port=port,
        factory=True,
    )


if __name__ == "__main__":
    main()
