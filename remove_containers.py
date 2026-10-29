#!/usr/bin/env python3
"""Compatibility entry point: stop containers without deleting them."""
from stop_containers import main


if __name__ == '__main__':
    raise SystemExit(main())
