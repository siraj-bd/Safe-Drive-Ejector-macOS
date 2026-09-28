"""
Platform Adapter Factory.
Detects current operating system and returns appropriate PlatformAdapter instance.
"""

import sys
from platform_adapters.base import PlatformAdapter


def get_platform_adapter() -> PlatformAdapter:
    """Returns an instance of MacOSAdapter."""
    from platform_adapters.macos import MacOSAdapter
    return MacOSAdapter()
