"""CLI entry: `astrocyte-gateway-py` or `python -m astrocyte_gateway`."""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    from astrocyte_gateway.observability import configure_process_logging

    configure_process_logging()

    host = os.environ.get("ASTROCYTE_HOST", "127.0.0.1")
    port = int(os.environ.get("ASTROCYTE_PORT", "8080"))

    uvicorn.run(
        "astrocyte_gateway.app:create_app",
        host=host,
        port=port,
        factory=True,
        access_log=False,
    )


if __name__ == "__main__":
    main()
