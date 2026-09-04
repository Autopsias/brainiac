"""EXC-03 — verifying the signed ``exceptions.json`` summary on the Cowork VM.

``exceptions_page.generate()`` (host, s07) writes a signed machine summary at
``<vault>/.brain/exceptions.json`` at the end of every ``brain maintain`` run.
This module is the VM-side reader that decides whether to TRUST it:
VERIFY-OR-UNREACHABLE (HARDENED:adv-2026-08-20) — any failure of the
contract below reports ``unreachable``, never a count, and never a
fabricated zero.

THE VERIFICATION CONTRACT (HARDENED:codex-verify-r1), ALL of these must
pass, checked in this order:

1. ``schema_version`` is not from the future (a version this reader does not
   understand is refused loudly, never guessed at).
2. the signature is valid against the PINNED public key — never a key read
   off the mount.
3. ``vault_id`` equals the PINNED workspace identity — never the mutable
   ``<vault>/.brain/vault-id`` file, which a compromised VM leg could rewrite
   right alongside a forged summary (this is what stops a cross-vault
   replay: a genuinely-signed summary from a DIFFERENT vault still fails
   here).
4. this reader's own engine version is not older than the summary's
   ``min_engine`` (version skew — a stale staged runtime says so instead of
   misreading a schema it predates).
5. freshness — the same staleness window ``alerts.py`` applies to the
   sibling ``notify-sent/current.json`` feed.
6. ``html_hash`` recomputed over the MOUNTED ``exceptions.html`` equals the
   signed value (HTML tamper: an edit to the page after signing is caught
   here, not silently served).

THE PIN. Both the public key and the vault_id are staged ONCE, at install
time, by the HOST-run ``tools/cowork_workspace_install.sh`` (``stage_pin``
below, run under the host Python that can resolve the audit signing key)
into the WORKSPACE runtime dir ``brain.cowork_staging.staging_root(vault,
workspace)`` names — never derived from ``exceptions.json`` itself and never
read from the mutable ``vault-id`` file. This is the SAME trust boundary the
staged ELF binaries and the bundled model already rely on (``doctor_vm.py``):
a Cowork session's ordinary CLI surface (``VM_ALLOWED``) never writes to this
path, so within that surface it is a fixed anchor. It is not a defense
against a fully compromised VM with arbitrary shell access rewriting its own
staged files — nothing in this system defends against that, and this module
does not claim to either.

MED-09 (2026-09-02): until this fix, the pin AND the summary/page it
verifies were all read from ``<vault>/.brain`` unconditionally — safe only
while the vault sits ON the mount. Once a vault relocates off the mount
(VULN-3385), that path is unreachable to the VM and ``verify`` always
reported ``unreachable``, never a fabricated count, but also never a real
one. Staging the pin alone at the relocation-aware root did not close this:
a real VM leg calls ``verify`` with ``vault`` already resolved to its own
workspace-local copy and reads the SUMMARY and the PAGE HASH through
``config.brain_runtime_dir(vault)`` too — which nothing had ever copied off
the real, off-mount vault. ``publish_summary`` (host-only, called after
every ``exceptions_page.generate()`` and at install/re-stage time) now
mirrors both files into the SAME relocation-aware staging root
``stage_pin`` uses; ``load_pinned`` and the summary/page readers all try
that location first and fall back to the legacy ``<vault>/.brain``
location — printing ONE warning line per artefact naming the new expected
path — for one release, so a workspace staged by an older installer is not
stranded mid-upgrade.

SCHEMA COMPATIBILITY IS BOTH DIRECTIONS (codex-verify-r2): a future schema
is refused loudly; a PAST schema is migrated explicitly via ``_MIGRATIONS``,
never refused. An unversioned pre-release summary (no ``schema_version`` key
at all — a hypothetical engine build that predates schema stamping) counts
as schema 0 and has its own fixture (``tests/fixtures/exceptions-v0.json``).
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from . import config as _config

PINNED_FILENAME = "pinned-verify.json"
MOUNT_HTML_FILENAME = "exceptions.html"
JSON_FILENAME = "exceptions.json"

# This reader understands exactly this schema number. Anything higher is a
# future format it must refuse rather than misread; anything lower is
# migrated via `_MIGRATIONS` below.
CURRENT_SCHEMA = 1

# Same window `alerts.FINDINGS_STALE_DAYS` uses for the sibling feed — kept
# as its own constant (not imported from `alerts`) because `alerts.py`
# imports THIS module, and a reverse import would cycle.
STALE_DAYS = 2


def _schema_number(raw: Any) -> int | None:
    """Parse ``"brain-exceptions/vN"`` -> ``N``. A missing/empty value is an
    unversioned pre-release state, which counts as schema 0 — its own
    migration fixture, never a refusal."""
    s = str(raw or "").strip()
    if not s:
        return 0
    if s.startswith("brain-exceptions/v"):
        try:
            return int(s.rsplit("v", 1)[-1])
        except ValueError:
            return None
    return None


def _migrate_v0(payload: dict[str, Any]) -> dict[str, Any]:
    """v0 = unversioned pre-release state: no ``egress_ceiling``, no
    ``min_engine``. Fill conservative defaults rather than refuse — old real
    data is still data, and refusing it would make a vault's very first
    upgraded maintain run report unreachable for no reason."""
    out = dict(payload)
    out.setdefault("egress_ceiling", "Internal")
    out.setdefault("min_engine", "0.0.0")
    return out


_MIGRATIONS = {0: _migrate_v0}


def _running_engine_version() -> str:
    try:
        from ._version import __version__

        return str(__version__) or "0.0.0"
    except Exception:  # noqa: BLE001 — an unreadable stamp reads as oldest
        return "0.0.0"


def _version_tuple(v: str) -> tuple[int, ...]:
    parts = []
    for p in str(v or "0").split("."):
        m = re.match(r"\d+", p)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts) or (0,)


def _parse_date(value: Any) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _load_pin_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("public_key_pem") or not data.get("vault_id"):
        return None
    return data


def load_pinned(vault: Path, workspace: Path | None = None) -> dict[str, Any] | None:
    """The staged identity anchor — ``None`` when this workspace was never
    staged with it (a pre-EXC-03 install, or a re-stage that failed).

    Tries the relocation-aware workspace runtime dir
    (``cowork_staging.staging_root(vault, workspace)``) FIRST — where
    ``stage_pin`` now writes — and falls back to the legacy
    ``<vault>/.brain`` location (MED-09), printing one warning line naming
    the new path so a workspace staged by an older installer is reported,
    never silently treated as never-staged."""
    from .cowork_staging import staging_root

    new_path = staging_root(vault, workspace) / PINNED_FILENAME
    data = _load_pin_file(new_path)
    if data is not None:
        return data

    old_path = _config.brain_runtime_dir(vault) / PINNED_FILENAME
    if old_path == new_path:
        return None
    data = _load_pin_file(old_path)
    if data is not None:
        print(
            f"[exceptions_verify] WARNING: pinned-verify.json not found at "
            f"{new_path} -- reading the legacy location {old_path} instead. "
            f"Re-run tools/cowork_workspace_install.sh to re-stage it there.",
            file=sys.stderr,
        )
    return data


def _resolve_mount_path(vault: Path, workspace: Path | None, filename: str) -> Path:
    """The relocation-aware MOUNT file location (MED-09), same posture as
    ``load_pinned``: try the workspace staging root first -- where
    ``publish_summary`` (host) now mirrors ``exceptions.json``/
    ``exceptions.html`` after every ``brain maintain`` run -- falling back to
    the legacy ``<vault>/.brain`` location for a workspace whose host has not
    re-run ``brain maintain``/the installer since upgrading, and printing ONE
    warning line so the fallback is never silent."""
    from .cowork_staging import staging_root

    new_path = staging_root(vault, workspace) / filename
    if new_path.exists():
        return new_path
    old_path = _config.brain_runtime_dir(vault) / filename
    if old_path != new_path and old_path.exists():
        print(
            f"[exceptions_verify] WARNING: {filename} not found at "
            f"{new_path} -- reading the legacy location {old_path} instead. "
            f"Re-run `brain maintain` on the host (or tools/"
            f"cowork_workspace_install.sh) to publish it there.",
            file=sys.stderr,
        )
        return old_path
    return new_path


def _load_summary(vault: Path, workspace: Path | None = None) -> dict[str, Any] | None:
    try:
        data = json.loads(_resolve_mount_path(vault, workspace, JSON_FILENAME)
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _verify_signature(raw: dict[str, Any], public_key_pem: str) -> bool:
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        pub = load_pem_public_key(public_key_pem.encode("utf-8"))
        sig = base64.b64decode(str(raw.get("signature") or ""))
        # The exact bytes `exceptions_page.build_json_summary` signed: the
        # payload WITHOUT the `signature` key, canonical JSON. Verify
        # against the RAW (pre-migration) dict — migration only fills
        # display defaults for reading, it must never change what the
        # signature covers.
        unsigned = {k: v for k, v in raw.items() if k != "signature"}
        canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
        pub.verify(sig, canonical)
        return True
    except Exception:  # noqa: BLE001 — any crypto failure is "not verified"
        return False


def _check_schema(raw: dict[str, Any]) -> tuple[int | None, str | None]:
    """Returns ``(schema, None)`` on a version this reader accepts, or
    ``(None, reason)`` on a malformed or future one."""
    schema = _schema_number(raw.get("schema_version"))
    if schema is None:
        return None, "host summary has a malformed schema_version"
    if schema > CURRENT_SCHEMA:
        return None, (f"host summary schema v{schema} is newer than this "
                      f"engine understands (v{CURRENT_SCHEMA}) — update the engine")
    return schema, None


def _check_signature(raw: dict[str, Any], pinned: dict[str, Any]) -> str | None:
    if not raw.get("signature"):
        return "host summary is unsigned"
    if not _verify_signature(raw, str(pinned["public_key_pem"])):
        return "host summary signature is invalid"
    return None


def _check_vault_id(raw: dict[str, Any], pinned: dict[str, Any]) -> str | None:
    if str(raw.get("vault_id") or "") != str(pinned["vault_id"]):
        return "host summary vault_id does not match this workspace"
    return None


def _check_version_skew(raw: dict[str, Any]) -> str | None:
    running = _running_engine_version()
    min_engine = str(raw.get("min_engine") or "0.0.0")
    if _version_tuple(running) < _version_tuple(min_engine):
        return (f"this runtime ({running}) is older than the summary's "
               f"min_engine ({min_engine}) — version skew, re-stage the workspace")
    return None


def _check_freshness(raw: dict[str, Any], today: datetime.date) -> str | None:
    at = _parse_date(raw.get("generated_at"))
    if at is None or (today - at).days > STALE_DAYS:
        return "host summary is stale or has no timestamp"
    return None


def _check_html_hash(
    raw: dict[str, Any], vault: Path, workspace: Path | None = None,
) -> str | None:
    try:
        html_bytes = _resolve_mount_path(vault, workspace, MOUNT_HTML_FILENAME).read_bytes()
    except OSError:
        return "mounted exceptions page is missing"
    if hashlib.sha256(html_bytes).hexdigest() != str(raw.get("html_hash") or ""):
        return "mounted exceptions page does not match its signed hash"
    return None


def verify(
    vault: Path, today: datetime.date, workspace: Path | None = None,
) -> tuple[bool, dict[str, Any] | None, str]:
    """Run the full VERIFICATION CONTRACT. Returns ``(ok, summary, reason)``:
    ``summary`` is the migrated payload on success, ``None`` on failure —
    the caller must never read a count out of a failed verification. Each
    check is its own small function so this stays a flat sequence, never a
    single branch-heavy block.

    ``workspace`` is optional and relocation-aware (MED-09): when given, the
    pin lookup prefers the workspace runtime dir over the legacy
    ``<vault>/.brain`` location. Omitting it preserves the pre-MED-09
    behaviour exactly (``None`` is a no-op for ``cowork_staging.staging_root``)."""
    pinned = load_pinned(vault, workspace)
    if pinned is None:
        return False, None, ("no pinned verification data staged for this "
                             "workspace — re-stage it (tools/"
                             "cowork_workspace_install.sh)")

    raw = _load_summary(vault, workspace)
    if raw is None:
        return False, None, "host summary missing or unparseable"

    schema, reason = _check_schema(raw)
    if reason is not None:
        return False, None, reason

    for check in (
        lambda: _check_signature(raw, pinned),
        lambda: _check_vault_id(raw, pinned),
        lambda: _check_version_skew(raw),
        lambda: _check_freshness(raw, today),
        lambda: _check_html_hash(raw, vault, workspace),
    ):
        reason = check()
        if reason is not None:
            return False, None, reason

    payload = raw
    for v in range(schema, CURRENT_SCHEMA):
        # A missing step is a BUG in this engine, not a bad summary — but it
        # must still REFUSE, never raise: `verify` runs from `brain alerts` at
        # session start, so a bare KeyError here would crash the degradation
        # digest on the first release that bumps CURRENT_SCHEMA and forgets a
        # migration. Fail closed, and name what is missing.
        step = _MIGRATIONS.get(v)
        if step is None:
            return False, None, (f"cannot migrate summary schema v{v} — this "
                                 f"engine declares v{CURRENT_SCHEMA} but ships "
                                 f"no migration for v{v}")
        payload = step(payload)
    return True, payload, "verified"


# ---------------------------------------------------------------------------
# Staging — HOST-ONLY. Called once at install/re-stage time
# (`tools/cowork_workspace_install.sh`), under a Python that can resolve the
# audit signing key. Never called from the VM leg.
# ---------------------------------------------------------------------------
def stage_pin(vault: Path, workspace: Path | None = None) -> dict[str, Any]:
    """Write the pin at the relocation-aware staging root (MED-09) — the
    SAME location every other runtime artefact (engine, model, skills) is
    staged at. ``workspace=None`` (the default, and every call site before
    MED-09) matches the old ``<vault>/.brain`` answer for the common case —
    but NOT byte-identically when ``$BRAIN_RUNTIME_DIR`` is set (a layout
    ``cowork_snapshot_guard.py`` documents as supported): the old code
    honoured that override and ``staging_root`` does not. The reader's
    legacy fallback still finds a pin staged under that override."""
    from . import audit as _audit
    from .cowork_staging import staging_root

    vid = _config.vault_id(vault, create=True) or ""
    pem = _audit.public_key_pem().decode("utf-8")
    data = {"vault_id": vid, "public_key_pem": pem}
    path = staging_root(vault, workspace) / PINNED_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return data


def publish_summary(vault: Path, workspace: Path | None = None) -> None:
    """Mirror ``exceptions.json`` + the MOUNT ``exceptions.html`` into the
    relocation-aware staging root (MED-09) — the SAME location ``stage_pin``
    writes the pin at.

    Staging the pin alone did not close MED-09: ``verify`` also reads the
    signed summary and recomputes the page hash, and both of those were
    still read from ``config.brain_runtime_dir(vault)`` — the REAL vault's
    own ``.brain``, off the mount once the vault relocates (VULN-3385). A
    real Cowork VM leg calls ``verify`` with ``vault`` already resolved to
    its OWN workspace-local copy (``$BRAIN_VAULT``, per
    ``cowork_session_bootstrap.sh``) and no ``workspace`` argument, so
    nothing ever copied the summary the host actually wrote into the one
    directory that call can reach. This closes that gap: HOST-ONLY, called
    right after ``exceptions_page.generate()`` writes the pair
    (``folds/reporting.py``, every ``brain maintain`` run) and once more at
    install/re-stage time (``tools/cowork_workspace_install.sh``).

    Co-located vaults are a no-op — ``staging_root(vault, None)`` is already
    ``<vault>/.brain``, the same directory ``generate()`` just wrote to.
    Best-effort: a missing source file (a vault that has never run `brain
    maintain`) is silently skipped, never a crash — the caller already
    wraps this in its own best-effort posture."""
    from .cowork_staging import staging_root

    src_dir = _config.brain_runtime_dir(vault)
    dest_dir = staging_root(vault, workspace)
    if src_dir == dest_dir:
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    for filename in (JSON_FILENAME, MOUNT_HTML_FILENAME):
        src = src_dir / filename
        if not src.exists():
            continue
        dest = dest_dir / filename
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(src.read_bytes())
        os.replace(tmp, dest)
