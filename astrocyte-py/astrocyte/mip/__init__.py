"""Memory Intent Protocol (MIP) — declarative memory routing.

See docs/_design/memory-intent-protocol.md for the design specification.

Lazy attribute loading (PEP 562) is used so that importing
`astrocyte.mip.schema` directly (e.g., from astrocyte.types) does not pull in
loader/router and create a circular import.
"""

from astrocyte.mip.schema import MipConfig

__all__ = ["MipRouter", "MipConfig", "load_mip_config"]


def __getattr__(name: str):  # noqa: ANN202
    if name == "MipRouter":
        from astrocyte.mip.router import MipRouter

        return MipRouter
    if name == "load_mip_config":
        from astrocyte.mip.loader import load_mip_config

        return load_mip_config
    raise AttributeError(f"module 'astrocyte.mip' has no attribute {name!r}")
