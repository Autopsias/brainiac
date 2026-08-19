"""Define reusable command argument bundles."""

from __future__ import annotations

import argparse

from .. import classification as cls


def add_common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--json", action="store_true", help="emit JSON")
    sp.add_argument(
        "--max-tier",
        default=None,
        choices=cls.TIERS,
        help="egress cap; results above this tier are withheld "
        f"(default: {cls.DEFAULT_MAX_TIER} on host, "
        f"{cls.VM_DEFAULT_MAX_TIER} on --role vm)",
    )
