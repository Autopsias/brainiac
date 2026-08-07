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

`--restamp` does the re-stamp itself, so a bundle upload and its BLOCKING pin
move are ONE command instead of a hand-edit that has to be remembered:
    python3 tools/cos_publish_pin.py --restamp --reason="..." <vault>
It sets `classifier.bundle_version` to the active skill's `kernel_version`,
records the move under a dated `repinned_*` key beside the existing history,
and republishes the projection in the same act. Idempotent: re-running once
canonical already matches prints "already stamped" and changes nothing.

ponytail: derived projection, not a second source of truth. `--check` exists so
a re-stamp that forgets to republish is detectable rather than silent.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import sys
from pathlib import Path

PROJECTION_NAME = "calibration-pin.json"
SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / ".claude" / "skills" / "chief-of-staff" / "SKILL.md"
)


def canonical_path(vault: Path) -> Path:
    return vault / ".brain" / "cos-ops" / "evidence" / "s05-calibration.json"


def projection_path(vault: Path) -> Path:
    return vault / ".brain" / "cos" / "shared" / PROJECTION_NAME


def skill_version(path: Path | None = None) -> str:
    path = path or SKILL_PATH
    match = re.search(
        r'^\s*kernel_version:\s*["\']([^"\']+)["\']\s*$',
        path.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if not match:
        raise ValueError(f"no metadata.kernel_version in {path}")
    return match.group(1)


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


def restamp(vault: Path, reason: str) -> int:
    """Move `classifier.bundle_version` to the active skill's kernel_version.

    Guard condition 4 is a STRING EQUALITY, so a bundle that ships without this
    move silently freezes every guard-4-gated phase (field precedent: run 37,
    2026-07-25 — `archived: 0` against `would_archive_count: 11`, every E-check
    green). Doing it here rather than by hand keeps the upload and the re-stamp
    in one act, and makes the no-op case explicit instead of a second edit.
    """
    src = canonical_path(vault)
    data = json.loads(src.read_text(encoding="utf-8"))
    active = skill_version()
    prev = data["classifier"]["bundle_version"]
    if prev == active:
        print(f"already stamped: canonical is {active!r} — nothing to do")
        return 0
    key = "repinned_{}_{}".format(
        _dt.date.today().isoformat().replace("-", "_"),
        re.sub(r"[^a-z0-9]", "", active.rsplit(" ", 1)[-1].lower()),
    )
    data["classifier"]["bundle_version"] = active
    data["classifier"][key] = f"{prev} -> {active}: {reason}"
    tmp = src.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(src)  # atomic: a half-written pin is never readable
    print(f"restamped {prev!r} -> {active!r} (history key: {key})")
    return 0


def main(argv: list[str]) -> int:
    # `--reason=TEXT` (equals form only) so a reason string can never be
    # mistaken for the positional <vault>.
    args = [a for a in argv[1:] if not a.startswith("--")]
    check = "--check" in argv
    do_restamp = "--restamp" in argv
    reason = next((a.split("=", 1)[1] for a in argv[1:]
                   if a.startswith("--reason=")), "")
    if not args:
        print("usage: cos_publish_pin.py [--check] "
              "[--restamp --reason=TEXT] <vault>", file=sys.stderr)
        return 2
    vault = Path(args[0]).expanduser().resolve()
    src = canonical_path(vault)
    if not src.exists():
        print(f"FAIL: no calibration record at {src}", file=sys.stderr)
        return 1
    if do_restamp:
        if check:
            print("FAIL: --restamp and --check are mutually exclusive",
                  file=sys.stderr)
            return 2
        if not reason.strip():
            print("FAIL: --restamp requires --reason=TEXT (what moved, and why "
                  "it is a re-stamp rather than a re-measure)", file=sys.stderr)
            return 2
        rc = restamp(vault, reason)
        if rc:
            return rc

    try:
        want = build(vault)
        active_version = skill_version()
    except (KeyError, json.JSONDecodeError, ValueError) as exc:
        print(f"FAIL: malformed calibration/skill version: {exc}", file=sys.stderr)
        return 1
    if want["bundle_version"] != active_version:
        print(
            f"STALE: canonical calibration is {want['bundle_version']!r}, "
            f"active skill is {active_version!r} — re-stamp canonical first",
            file=sys.stderr,
        )
        return 1
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
        print(
            f"OK: projection, canonical, and active skill match "
            f"({want['bundle_version']})"
        )
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
        version = skill_version()
        c.write_text(json.dumps({"classifier": {"bundle_version": version}}))
        assert main(["x", str(v)]) == 0
        assert main(["x", "--check", str(v)]) == 0
        # re-stamp the canonical WITHOUT republishing -> must be detected
        c.write_text(json.dumps({"classifier": {"bundle_version": version + "-stale"}}))
        assert main(["x", "--check", str(v)]) == 1, "stale projection went undetected"
        c.write_text(json.dumps({"classifier": {"bundle_version": version}}))
        assert main(["x", str(v)]) == 0
        assert main(["x", "--check", str(v)]) == 0
        assert json.loads(projection_path(v).read_text())["bundle_version"] == version

        # --restamp: a stale canonical must be moved AND republished in one act,
        # the history preserved, and a second run must be a no-op.
        c.write_text(json.dumps({"classifier": {"bundle_version": "old v0.0",
                                                "repinned_earlier": "keep me"}}))
        assert main(["x", "--check", str(v)]) == 1, "stale canonical went undetected"
        assert main(["x", "--restamp", str(v)]) == 2, "--restamp accepted no reason"
        assert main(["x", "--restamp", "--reason=t", str(v)]) == 0
        assert main(["x", "--check", str(v)]) == 0, "restamp did not republish"
        after = json.loads(c.read_text())["classifier"]
        assert after["bundle_version"] == version
        assert after["repinned_earlier"] == "keep me", "restamp dropped history"
        assert any(k.startswith("repinned_2") and "old v0.0" in after[k]
                   for k in after), "restamp recorded no dated history key"
        assert main(["x", "--restamp", "--reason=t", str(v)]) == 0  # idempotent
    print("selfcheck OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        raise SystemExit(main(sys.argv))
