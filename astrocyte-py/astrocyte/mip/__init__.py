"""Memory Intent Protocol (MIP) — declarative memory routing.

See docs/_design/memory-intent-protocol.md for the design specification.

:class:`MipRouter` and :func:`load_mip_config` are lazy-loaded via PEP 562
``__getattr__`` — ``astrocyte.types`` imports ``astrocyte.mip.schema`` at
module scope for :class:`RoutingDecision` fields, so eagerly importing
``router`` / ``loader`` here creates a circular import.

Only names actually defined at module scope appear in ``__all__`` (CodeQL's
``py/undefined-export`` rule does not recognize ``__getattr__``-resolved
names, and statically the names are genuinely undefined). ``from
astrocyte.mip import MipRouter`` still works — Python's attribute lookup
falls through to ``__getattr__``.
"""

from astrocyte.mip.schema import MipConfig

__all__ = ["MipConfig"]  # Lazy names: MipRouter, load_mip_config (see __getattr__)


def __getattr__(name: str):  # noqa: ANN202
    if name == "MipRouter":
        from astrocyte.mip.router import MipRouter

        return MipRouter
    if name == "load_mip_config":
        from astrocyte.mip.loader import load_mip_config

        return load_mip_config
    raise AttributeError(f"module 'astrocyte.mip' has no attribute {name!r}")
