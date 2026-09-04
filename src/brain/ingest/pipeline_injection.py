"""SEC-05 admission stage: refuse extracted text that CONCEALS an instruction.

Its own module because the stage list lives under a 500-LOC file bound and
this is a self-contained gate — the same reason ``pipeline_duplicates`` and
``pipeline_files`` are separate.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .pipeline_stages import ClaimRecord


def injection_scan_stage(record: ClaimRecord) -> ClaimRecord:
    """Refuse extracted text that CONCEALS an instruction (SEC-05).

    Runs on the handler's Markdown, before autolinks and before the note is
    signed, so a concealed instruction never reaches the index in the first
    place. Anthropic's prompt-injection probes inspect a tool result at READ
    time (Opus 5 System Card §5.2); nothing inspects the corpus at WRITE time,
    and the corpus is what an ingesting second brain accumulates.

    Only the ``conceal`` verdict is terminal. ``instruction_only`` — a document
    that merely QUOTES an attack, like a pentest report — is recorded and
    ingested, because a scanner that eats the security report is worse than no
    scanner. See brain.injection_scan for the measurement behind that split.
    """
    if record.terminal:
        return record
    from .. import injection_scan
    # Imported INSIDE the call: pipeline_stages imports this module at load, so
    # a module-level import back would be circular.
    from .pipeline_stages import _quarantine_handler_failure

    assert record.result is not None
    verdict = injection_scan.scan(record.result.markdown or "")
    # Second half of the scan (M-3, 2026-09-02): the handler saw colour, size
    # and position; this line is where "that text was hidden" meets the
    # instruction patterns. Styling is already gone from `markdown` above.
    verdict = injection_scan.fold_concealed(
        verdict, record.result.metadata.get("concealed") or [])
    # Whether the walk above RAN. The handler ATTESTS it in its own metadata
    # (V9, 2026-09-02); the warnings are a second, downgrade-only input.
    # Without this a note ingested with the kill switch off — or by one of the
    # five lanes that never walk — is stamped `hidden: 0` and reads exactly
    # like one that was searched and found clean.
    verdict["concealment_scan"] = injection_scan.concealment_scan_state(
        record.result.metadata, record.result.warnings)
    record.injection = verdict
    if injection_scan.should_quarantine(verdict):
        markers = ", ".join(
            sorted({str(m.get("marker")) for m in verdict["concealment"]})
        )
        return _quarantine_handler_failure(
            record,
            injection_scan.QUARANTINE_REASON,
            [f"concealed instruction markers: {markers}",
             "The file is intact in the quarantine dir. Review it by hand: "
             "text hidden from a human reader but visible to a model is the "
             "shape of an indirect prompt injection."],
        )
    # An ``instruction_only`` verdict is deliberately NOT an ingest event: the
    # document is admitted, and ``record.injection`` carries the markers for any
    # caller that wants them. `brain integrity --injection` is the reporting
    # surface (the flag is real; only this comment's command name was wrong).
    #
    # The assessment rides onto the note as FLAT `injection_assessment.*`
    # keys — see `apply_injection_assessment`, which is called from
    # `tierguard_stage` and NOT from here. This stage cannot stamp them
    # itself: `tierguard_stage` runs next and REPLACES `record.meta` wholesale
    # (`record.meta = facade._meta(...)`), so anything written here is
    # discarded before the note is built. Measured, not assumed.
    return record


def apply_injection_assessment(record: ClaimRecord) -> None:
    """Stamp the concealment assessment onto ``record.meta`` (M-3).

    Called from ``tierguard_stage``, immediately after it rebuilds
    ``record.meta`` and before ``signed_note_write_stage`` serializes it — so
    the keys are inside the signature and the audit-chain hash rather than a
    later unsigned edit. Counts and marker names only; the hidden text is
    already in the signed body, and a second copy in frontmatter would put
    attacker-authored text where the retrieval layer serves it.
    """
    if record.injection is None:
        return
    from .. import injection_scan

    record.meta.update(injection_scan.assessment_meta(record.injection))
