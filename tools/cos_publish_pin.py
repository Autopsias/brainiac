#!/usr/bin/env python3
"""Publish the COS calibration pin as a VM-READABLE projection.

WHY THIS EXISTS (measured 2026-07-25, run 37). Phase 1.5 guard condition 4
compares `classifier.bundle_version` in the calibration record against the
skill's own `metadata.kernel_version`. That record lives at the LEGACY path
`<vault>/.brain/cos-ops/evidence/s05-calibration.json` — which is outside the
engine's `.brain/cos/` tree, so it is in neither the host-private `host/` zone
nor the VM-readable `shared/` zone. E9 permits the VM leg exactly one `.brain/`
read (`shared/priority-map.md`), so a `--role vm` run could not satisfy guard 4
WITHOUT breaching E9 — auto-archive was unsatisfiable by construction, which is
why every VM-leg run reported `archived: 0` while `would_archive_count` sat at
11. Run 37 refused to read it and held, correctly.

This publishes a read-only projection into `shared/` — the documented
"host writes, VM reads" zone, alongside priority-map.md — so guard 4 becomes
checkable without widening the read scope. The canonical record stays the SSOT
and is never moved; this is a derived copy, exactly like priority-map.md.

Run it on the HOST after any pin re-stamp:
    python3 tools/cos_publish_pin.py <vault>
    python3 tools/cos_publish_pin.py --check <vault>   # verify, write nothing

ponytail: derived projection, not a second source of truth. `--check` exists so
a re-stamp that forgets to republish is detectable rather than silent.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

PROJECTION_NAME = "calibration-pin.json"


def canonical_path(vault: Path) -> Path:
    return vault / ".brain" / "cos-ops" / "evidence" / "s05-calibration.json"


def projection_path(vault: Path) -> Path:
    return vault / ".brain" / "cos" / "shared" / PROJECTION_NAME


def build(vault: Path) -> dict:
    src = canonical_path(vault)
    raw = src.read_bytes()
    pin = json.loads(raw)["classifier"]["bundle_version"]
    return {
        "bundle_version": pin,
        "source": str(src.relative_to(vault)),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "note": (
            "DERIVED, read-only projection of the calibration pin for the VM leg "
            "(guard condition 4). The canonical record named in `source` is the SSOT; "
            "re-stamp there, then re-run tools/cos_publish_pin.py. A stale projection "
            "is a HOLD, never a pass — a mismatch against the skill's kernel_version "
            "freezes auto-archive exactly as an unreadable pin does."
        ),
    }


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    check = "--check" in argv
    if not args:
        print(__doc__.strip().splitlines()[-4], file=sys.stderr)
        print("usage: cos_publish_pin.py [--check] <vault>", file=sys.stderr)
        return 2
    vault = Path(args[0]).expanduser().resolve()
    src = canonical_path(vault)
    if not src.exists():
        print(f"FAIL: no calibration record at {src}", file=sys.stderr)
        return 1

    want = build(vault)
    dst = projection_path(vault)

    if check:
        if not dst.exists():
            print(f"STALE: no projection at {dst} — guard 4 is unsatisfiable on the VM leg")
            return 1
        have = json.loads(dst.read_text())
        if have.get("source_sha256") != want["source_sha256"]:
            print(f"STALE: projection is {have.get('bundle_version')!r}, "
                  f"canonical is {want['bundle_version']!r} — re-run without --check")
            return 1
        print(f"OK: projection matches canonical ({want['bundle_version']})")
        return 0

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(want, indent=2) + "\n", encoding="utf-8")
    tmp.replace(dst)  # atomic: the VM never sees a half-written pin
    dst.chmod(0o644)
    print(f"published {want['bundle_version']} -> {dst}")
    return 0


def _selfcheck() -> None:
    """assert-based check: a re-stamp that isn't republished must read STALE."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        v = Path(td)
        c = canonical_path(v)
        c.parent.mkdir(parents=True)
        c.write_text(json.dumps({"classifier": {"bundle_version": "chief-of-staff v1.0"}}))
        assert main(["x", str(v)]) == 0
        assert main(["x", "--check", str(v)]) == 0
        # re-stamp the canonical WITHOUT republishing -> must be detected
        c.write_text(json.dumps({"classifier": {"bundle_version": "chief-of-staff v2.0"}}))
        assert main(["x", "--check", str(v)]) == 1, "stale projection went undetected"
        assert main(["x", str(v)]) == 0
        assert main(["x", "--check", str(v)]) == 0
        assert json.loads(projection_path(v).read_text())["bundle_version"] == "chief-of-staff v2.0"
    print("selfcheck OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        raise SystemExit(main(sys.argv))
