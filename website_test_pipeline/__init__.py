"""Python implementation of the dynamic website test pipeline."""

__version__ = "0.1.0"

# a run the API started may only browse the public internet (netguard.py); a no-op for anything else
from . import netguard as _netguard

_netguard.maybe_install()
