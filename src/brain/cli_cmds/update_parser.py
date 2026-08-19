"""Register release-refresh commands."""

from __future__ import annotations


def _add_update(sub) -> None:
    sp = sub.add_parser(
        "update",
        help="the ONE 'get current' command (ADR-0005 Ruling 3, UP-01/UP-02): marketplace refresh -> downgrade-safe CLI-plugin reinstall -> engine venv refresh -> workspace re-stage -> `brain doctor` verify, one before->after table, one pass/fail (host only)",
    )
    sp.add_argument(
        "--marketplace",
        default="brainiac",
        help="marketplace name to refresh/compare against (default: %(default)s)",
    )
    sp.add_argument(
        "--engine-src",
        default=None,
        help="engine checkout to install -e from (default: resolved from $BRAINIAC_ENGINE_SRC, else this repo's own root)",
    )
    sp.add_argument(
        "--dry-run",
        action="store_true",
        help="run every read/decision step for real but skip every mutating call (marketplace update, plugin install/uninstall, pip install, workspace re-stage) — prints what WOULD happen",
    )
    sp.add_argument(
        "--skip-capability-probe",
        action="store_true",
        help="skip the claude-plugin-CLI preflight probe (debugging only)",
    )
    sp.add_argument("--json", action="store_true")


def add_parser(sub) -> None:
    _add_update(sub)
