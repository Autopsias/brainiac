"""COS hold-store operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._approval import _identity_binds, approved_vault_identity, approved_verify_key
from ._guards import _safe_meta_id
from ._io import _write_atomic
from ._layout import _parse_ts, _ts, _utcnow, hold_dir

def _hold_body(nid: str, sha: str, not_before: str, created: str,
               authorization: str, vault_identity: str) -> str:
    return json.dumps({"schema": HOLD_RECORD_SCHEMA, "id": nid, "sha256": sha,
                       "not_before": not_before, "created": created,
                       "authorization": authorization, "vault": vault_identity},
                      sort_keys=True, separators=(",", ":"))

def verified_hold(vault, marker: dict[str, Any], *, nid: str | None = None,
                  pubkey=None) -> dict[str, Any] | None:
    """The signed authorization inside a hold marker, or ``None``.

    ``None`` covers missing (a legacy unsigned hold), malformed, badly signed,
    wrong-schema and foreign-vault records alike: none of them is an
    authorization this host issued, and the answer at the release gate is the
    same for all of them — refuse."""
    if not isinstance(marker, dict) or not isinstance(marker.get("body"), str):
        return None
    if pubkey is None:
        pubkey = public("approved_verify_key")(vault)      # may raise KeyUnavailable
    try:
        pubkey.verify(bytes.fromhex(str(marker.get("sig", ""))),
                      marker["body"].encode("utf-8"))
        body = json.loads(marker["body"])
    except Exception:  # noqa: BLE001 — unverifiable is unusable
        return None
    if (not isinstance(body, dict)
            or body.get("schema") != HOLD_RECORD_SCHEMA
            or body.get("id") != (nid if nid is not None else marker.get("id"))
            or not _identity_binds(vault, body)
            or not isinstance(body.get("sha256"), str)
            or not _parse_ts(str(body.get("not_before", "")))):
        return None
    return body

def hold_add(vault, content: str, *, not_before: str,
             ident: str | None = None,
             evidence: dict[str, Any] | None = None,
             authorization: str = "auto-capture") -> dict[str, Any]:
    """Park a qualifying auto-capture item UNSIGNED until ``not_before``.

    The item enters the approved queue (and thence the signed drain) ONLY
    after the stated interval expires — the undo window. Cancellation before expiry
    is atomic (see ``hold_cancel``). ``evidence`` carries the graduation key
    this item was auto-captured under, so an undo can demote its category.

    The PAYLOAD is unsigned (it is not a vault note yet), but the RECORD is
    signed here: it is the host's authorization, and release refuses without
    it. Fails closed if no key resolves — nothing is parked."""
    from .. import audit
    from .. import capture as cap_mod

    nb = _parse_ts(not_before)
    if nb is None:
        raise ValueError(f"not_before must be an ISO timestamp, got {not_before!r}")
    meta, _ = frontmatter.parse_text(content)
    nid = safe_slug(ident or meta.get("id") or ("hold-" + sha256_text(content)[:12]))
    staged = cap_mod.enforce(content, override={"id": nid})
    hdir = hold_dir(vault)
    hdir.mkdir(parents=True, exist_ok=True)
    md = hdir / f"{nid}.md"
    marker = hdir / f"{nid}.hold.json"
    if md.exists() or marker.exists():
        raise ValueError(f"hold already exists for id {nid!r}")
    created = _ts()
    body = _hold_body(nid, sha256_text(staged), _ts(nb), created, authorization,
                      approved_vault_identity(vault))
    key_obj, _src = audit.resolve_signing_key()   # KeyUnavailable -> nothing parked
    sig = key_obj.sign(body.encode("utf-8")).hex()
    public("_write_atomic")(md, staged.encode("utf-8"))
    public("_write_atomic")(marker, (json.dumps(
        {"id": nid, "not_before": _ts(nb), "created": created,
         "evidence": evidence or {}, "body": body, "sig": sig},
        sort_keys=True) + "\n").encode("utf-8"))
    return {"id": nid, "not_before": _ts(nb), "path": str(md), "signed": False,
            "authorized": True}

def hold_list(vault, now: _dt.datetime | None = None) -> list[dict[str, Any]]:
    now = now or _utcnow()
    out = []
    hdir = hold_dir(vault)
    if not hdir.is_dir():
        return out
    for marker in sorted(hdir.glob("*.hold.json")):
        try:
            m = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        nid = _safe_meta_id(m)
        if nid is None:
            continue          # an unusable id is an unusable hold, not a path
        nb = _parse_ts(m.get("not_before", ""))
        m = {**m, "id": nid, "due": bool(nb and nb <= now)}
        out.append(m)
    return out

__all__ = ['_hold_body', 'verified_hold', 'hold_add', 'hold_list']
