"""Memory Intent Protocol (MIP) — declarative memory routing.

See docs/_design/memory-intent-protocol.md for the design specification.
"""

from astrocyte.mip.loader import load_mip_config
from astrocyte.mip.router import MipRouter
from astrocyte.mip.schema import MipConfig

__all__ = ["MipRouter", "MipConfig", "load_mip_config"]
