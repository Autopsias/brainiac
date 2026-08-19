"""COS proposal-ingress operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._io import _write_atomic
from ._layout import proposal_drop_dir, verdict_drop_dir

def propose(vault, content: str, *, ident: str | None = None) -> dict[str, Any]:
    """Write ONE unsigned proposal candidate into ``drop/proposal-drop/``.

    VM-ALLOWED. Never signs, never indexes, never touches capture-inbox — the
    ordinary ``brain sync`` drain does not read this directory, so nothing
    dropped here can reach the signed write path without the broker.

    PRV-01: email provenance travels in the candidate's own frontmatter as the
    flat dotted keys ``provenance.sender``/``.sent``/``.conversation_id``/
    ``.subject``. ``capture.enforce`` sanitizes + secret-scrubs them and strips
    any ``provenance.verified`` a VM tried to assert; from there they are
    preserved untouched through claim, batch, accept and the signed drain.
    """
    from .. import capture as cap_mod

    meta, _body = frontmatter.parse_text(content)
    note_id = ident or (str(meta.get("id")) if meta and meta.get("id") else None)
    if not note_id:
        note_id = "cosprop-" + sha256_text(content)[:12]
    note_id = safe_slug(note_id)  # C-1 fail-closed on traversal ids
    # ING-03 fix: capture.enforce()'s generic default (Internal, UX-01) is wrong
    # here — Phase 1.6 requires ingestion candidates to default to MNPI
    # (most-restrictive) unless the candidate content itself states a tier.
    # Malformed/double-frontmatter candidate content (observed 3/10 in the
    # 2026-07-14/15 window) silently fell through to Internal without this.
    cls_override = meta.get("classification") or "MNPI"
    staged = cap_mod.enforce(
        content, override={"id": note_id, "classification": cls_override})
    # STA-01: the producer-version stamps are the HOST's to derive (from the
    # run manifest), so they come off here too — the same keys the claim strips.
    # Doing it at the ingress as well means the sha this call REPORTS is the sha
    # the host will compute, so an honest producer's ledger row joins by
    # construction; a raw drop-dir writer that still asserts them gets a digest
    # mismatch, which is the loud outcome that shape deserves.
    try:
        staged = provenance.without_host_only_text(staged, keys=_STRIPPED_CLAIM_KEYS)
    except provenance.HostOnlyKeyResidue as exc:
        raise ValueError(f"candidate smuggles a host-derived key: {exc}") from exc
    ddir = proposal_drop_dir(vault)
    ddir.mkdir(parents=True, exist_ok=True)
    target = ddir / f"{note_id}.md"
    if target.resolve().parent != ddir.resolve():
        raise ValueError(f"proposal target escapes drop dir: {note_id!r}")
    public("_write_atomic")(target, staged.encode("utf-8"), mode=MODE_VM_WRITABLE)
    return {"proposal": str(target), "id": note_id, "signed": False,
            "state": "dropped",
            # The run copies BOTH into its ingestion-ledger row: the host joins
            # the category back by (id + full content digest), and a row
            # carrying only the id proves nothing about these bytes.
            "sha256": sha256_text(staged),
            "note": "unsigned proposal drop; the host broker validates, asks the "
                    "owner, and only an ACCEPTED candidate is ever signed. Record "
                    "`id` + `sha256` in this run's ingestion-ledger row with the "
                    "category — the host joins them there, and an unjoinable "
                    "candidate is quarantined, never silently unclassified"}

def propose_correction(vault, payload: dict[str, Any]) -> dict[str, Any]:
    """VM-ALLOWED: drop ONE correction request into ``drop/verdict-drop/``.

    This is the defined transport for the owner's one-line Cowork correction
    (see docs/cos-ops.md): VM drop → host broker validates against the shadow
    ledger → owner-inbox question → the ANSWER (the human act on the host) is
    what inserts the ``correction_events`` row. A VM write alone never mutates
    the corrections store of record."""
    errs = _validate_correction_payload(payload)
    if errs:
        raise ValueError("invalid correction payload: " + "; ".join(errs))
    ddir = verdict_drop_dir(vault)
    ddir.mkdir(parents=True, exist_ok=True)
    name = f"correction-{payload['round']}-{safe_slug(payload['msg_key'])}.json"
    target = ddir / name
    public("_write_atomic")(target, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"),
                  mode=MODE_VM_WRITABLE)
    return {"drop": str(target), "state": "dropped",
            "note": "correction drop staged; the host broker will surface it as "
                    "an owner-inbox question — a VM write never mutates the "
                    "corrections store of record"}

def _validate_correction_payload(p: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(p, dict):
        return ["payload must be a JSON object"]
    if not isinstance(p.get("round"), int):
        errs.append("round must be an integer")
    for k in ("msg_key", "corrected_bucket", "corrected_tier"):
        v = p.get(k)
        if not isinstance(v, str) or not v.strip():
            errs.append(f"{k} must be a non-empty string")
    return errs

__all__ = ['propose', 'propose_correction', '_validate_correction_payload']
