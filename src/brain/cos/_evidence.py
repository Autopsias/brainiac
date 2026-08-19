"""COS evidence operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._io import _write_atomic
from ._layout import _ts, _utcnow, evidence_dir, verdict_drop_dir

def _canonical_manifest(manifest: dict[str, Any]) -> str:
    unsigned = {k: v for k, v in manifest.items() if k not in ("sig", "public_key_pem")}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"))

def source_ledger_hash(vault) -> str:
    """sha256 over the (sorted) shadow-ledger drop bytes, or ``"none"``."""
    vdir = verdict_drop_dir(vault)
    files = sorted(vdir.glob("shadow-ledger*.jsonl")) if vdir.is_dir() else []
    if not files:
        return "none"
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode("utf-8"))
        h.update(f.read_bytes())
    return h.hexdigest()

def sign_evidence(vault, *, bundle_version: str, model_version: str,
                  dataset_window: str, files: list[Path] | None = None,
                  snapshot_generation: Any = None, name: str = "evidence",
                  now: _dt.datetime | None = None) -> dict[str, Any]:
    """Write a trust-gate evidence bundle under ``host/evidence/`` with a
    SIGNED, versioned manifest binding bundle version, model version, snapshot
    generation, dataset window, and the source-ledger hash. HOST-only (the
    caller gates); fails closed without a signing key."""
    from .. import audit
    from ..snapshot import read_manifest

    now = now or _utcnow()
    if snapshot_generation is None:
        snap = read_manifest(config.snapshot_dir(vault))
        snapshot_generation = getattr(snap, "generation", None)
    dest = evidence_dir(vault) / f"{safe_slug(name)}-{now.strftime('%Y%m%dT%H%M%SZ')}"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dest, 0o700)  # nosemgrep: insecure-file-permissions -- intentionally OWNER-ONLY (evidence dir), not overly-permissive
    except OSError:
        pass
    file_hashes: dict[str, str] = {}
    for f in files or []:
        f = Path(f)
        data = f.read_bytes()
        shutil.copy2(f, dest / f.name)
        file_hashes[f.name] = hashlib.sha256(data).hexdigest()
    manifest: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "bundle_version": bundle_version,
        "model_version": model_version,
        "snapshot_generation": snapshot_generation,
        "dataset_window": dataset_window,
        "source_ledger_hash": source_ledger_hash(vault),
        "created": _ts(now),
        "files": file_hashes,
    }
    key_obj, source = audit.resolve_signing_key()  # KeyUnavailable → fail closed
    manifest["sig"] = key_obj.sign(_canonical_manifest(manifest).encode("utf-8")).hex()
    manifest["public_key_pem"] = audit.public_key_pem().decode("ascii")
    public("_write_atomic")(dest / "manifest.json",
                  (json.dumps(manifest, indent=2, sort_keys=True) + "\n")
                  .encode("utf-8"))
    config.secure_file_permissions(dest / "manifest.json")
    return {"dir": str(dest), "manifest": str(dest / "manifest.json"),
            "signed_with": source, "snapshot_generation": snapshot_generation}

def verify_evidence(bundle_dir: Path | str) -> dict[str, Any]:
    """Verify an evidence bundle: manifest signature (against the HOST key —
    never the manifest's own embedded key) + every payload file hash. A
    stale/edited JSON or payload fails."""
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    from .. import audit

    bundle_dir = Path(bundle_dir)
    errors: list[str] = []
    mpath = bundle_dir / "manifest.json"
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "errors": [f"manifest unreadable: {exc}"]}
    try:
        pub = load_pem_public_key(audit.public_key_pem())
        pub.verify(bytes.fromhex(manifest.get("sig", "")),
                   _canonical_manifest(manifest).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — any failure = invalid signature
        errors.append(f"signature verification failed: {type(exc).__name__}: {exc}")
    if manifest.get("schema") != EVIDENCE_SCHEMA:
        errors.append(f"unexpected schema: {manifest.get('schema')!r}")
    for fname, expected in (manifest.get("files") or {}).items():
        if not fname or Path(str(fname)).name != str(fname):
            errors.append(f"payload name is not a bare filename: {fname!r}")
            continue
        fpath = bundle_dir / fname
        if not fpath.exists():
            errors.append(f"payload missing: {fname}")
            continue
        actual = hashlib.sha256(fpath.read_bytes()).hexdigest()
        if actual != expected:
            errors.append(f"payload hash mismatch: {fname}")
    return {"ok": not errors, "errors": errors,
            "manifest": {k: manifest.get(k) for k in
                         ("schema", "bundle_version", "model_version",
                          "snapshot_generation", "dataset_window",
                          "source_ledger_hash", "created")}}

__all__ = ['_canonical_manifest', 'source_ledger_hash', 'sign_evidence', 'verify_evidence']
