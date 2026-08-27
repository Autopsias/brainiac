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
    # caller that wants them. `brain check --injection` is the reporting surface.
    return record
