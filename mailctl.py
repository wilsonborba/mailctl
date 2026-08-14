#!/usr/bin/env python3
"""Compatibility shim for the refactored mailctl application."""

import sys

from mailctl_app import legacy as _legacy

sys.modules[__name__] = _legacy


if __name__ == "__main__":
    raise SystemExit(_legacy.main())
