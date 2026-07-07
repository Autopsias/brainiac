"""ING-01/ING-02 — document ingestion pipeline (ADR-0003 Ruling 1).

Drop a file into ``<vault>/inbox/``; the HOST-only ``brain ingest`` verb (also
folded into every host ``brain sync``, per the s01 cadence amendment) extracts
it to Markdown, archives the untouched original immutably under
``raw/originals/``, and commits the extracted source through the existing
audited ``write_note`` path. Unhandled/failed files quarantine to
``inbox/_quarantine/<reason>/`` — never silently dropped.

Public surface: :func:`run_ingest` (orchestrator) and :func:`capability_report`
(which handlers are usable given installed deps) — both re-exported from
``.pipeline`` and ``.handlers``.
"""
from __future__ import annotations

from .handlers import ExtractResult, Handler, capability_report
from .pipeline import inbox_dir, run_ingest

__all__ = ["ExtractResult", "Handler", "capability_report", "inbox_dir", "run_ingest"]
