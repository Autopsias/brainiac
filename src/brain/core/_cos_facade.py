"""COS delegation methods for BrainCore."""
from __future__ import annotations

from ._shared import (
    Any,
    Path,
    config,
)


class _CosFacadeMixin:
    """COS delegation methods for BrainCore."""

    def cos_propose(self, content: str, *, ident: str | None = None) -> dict[str, Any]:
        """VM-ALLOWED unsigned proposal ingress — writes to the proposal-drop
        dir that ``sync`` NEVER reads. Available on both legs (like
        ``draft_capture``); the broker/owner gate is what makes it safe."""
        from .. import cos as cos_mod

        return cos_mod.propose(self.vault, content, ident=ident)
    def cos_propose_correction(self, payload: dict[str, Any]) -> dict[str, Any]:
        """VM-ALLOWED correction drop (verdict-drop/) — see docs/cos-ops.md."""
        from .. import cos as cos_mod

        return cos_mod.propose_correction(self.vault, payload)
    def cos_run_begin(self, *, run_id: str | None = None,
                      lane: str | None = None,
                      skill_path: str | None = None,
                      attended: bool = False) -> dict[str, Any]:
        """HOST-ONLY (STA-01): freeze the run manifest at run LAUNCH.

        The manifest — run id, resolved SKILL.md path + content digest, both
        producer versions, the artifacts the run owes — is what later stamps
        that run's candidates. `claim_drops` fires hourly and the deployed
        bundle changes between runs, so reading the version at CLAIM time would
        stamp a proposal with a bundle that did not produce it.

        An EXPLICIT ``run_id`` dated other than today is refused here, at the
        operator entry point rather than in ``write_run_manifest`` (fixtures
        and reconstruction legitimately build fixed-date manifests). A run
        executes under the CALENDAR date, so a manifest dated otherwise splits
        the run's identity: measured 2026-08-06/07, run 74 was begun a day
        early, wrote its corpus under the manifest id and its ledgers under the
        calendar id, and left 50 verified marks permanently unappendable
        because ``host_stamps`` found no manifest at the id the ledgers used.
        """
        from .. import cos as cos_mod

        self._require_host("begin a COS run (write the run manifest)")
        if run_id:
            today = cos_mod.utcnow().strftime("%Y-%m-%d")
            if str(run_id)[:10] != today:
                raise ValueError(
                    f"refusing to begin {run_id}: its date is not today "
                    f"({today}). A run executes under the calendar date, so a "
                    f"manifest dated otherwise splits the run's identity — the "
                    f"corpus lands under the manifest id, the ledgers under the "
                    f"calendar id, and the metrics row can then be appended for "
                    f"neither. Begin the run on the day it runs.")
        return cos_mod.write_run_manifest(self.vault, run_id=run_id, lane=lane,
                                          skill_path=skill_path,
                                          attended=attended)
    def cos_corpus_check(self, run_id: str) -> dict[str, Any]:
        """HOST-ONLY (WIR-02): may this run's Phase 1.6 judgment start?

        The judge's input is the message body. This reports how many of the
        run's captured threads actually carry one, and REFUSES
        (:class:`brain.cos_corpus.NoBodiesToJudge`) when none does — the run-65
        shape, where a body pass that never executed still produced 58
        ``no-substance`` verdicts. Judging cannot honestly begin from there.

        A corpus with SOME bodyless rows is normal (an unread thread is never
        opened, and opens are capped) and passes; the count comes back so the
        run states its denominator rather than implying one."""
        from .. import cos_corpus as corpus_mod

        self._require_host("check the COS capture corpus before judging")
        rows = corpus_mod.read_corpus(self.vault, run_id)
        _, report = corpus_mod.judgeable(rows, source=f"run {run_id}")
        return report
    def cos_corpus_append(self, run_id: str, rows: list[dict[str, Any]]
                          ) -> dict[str, Any]:
        """HOST-ONLY (WIR-01): save the text the run just READ.

        The nightly extracts a message body, judges it, and throws the text
        away — so re-judging anything costs a 90-minute live run, and a run
        that never read at all is indistinguishable from one that read and
        found nothing. This is the call that makes the reading leave evidence.

        Takes a LIST because the two shapes are different work: the row for an
        opened body is written one at a time, in the same breath as the open,
        while the rows for threads that were enumerated and never opened carry
        no text and so have nothing to lose by going in one batch.
        """
        from .. import cos_corpus as corpus_mod

        self._require_host("append to the COS capture corpus")
        written: list[dict[str, Any]] = []
        for row in rows:
            try:
                written.append(corpus_mod.append_thread(self.vault, run_id, **row))
            except corpus_mod.CorpusRefused as exc:
                # NAME WHERE IT STOPPED. The corpus is append-only, so a caller
                # told only "refused" re-sends the whole batch and duplicates
                # every row that already landed.
                raise type(exc)(
                    f"{exc} — {len(written)} of {len(rows)} row(s) were "
                    f"appended before this one; re-send only the rest"
                ) from None
        return {"run": run_id, "appended": len(written),
                "conversation_ids": [w["conversation_id"] for w in written],
                "chars": sum(w["chars"] for w in written)}
    def cos_corpus_close(self, run_id: str) -> dict[str, Any]:
        """HOST-ONLY (WIR-01): close this run's corpus.

        Retention only deletes CLOSED corpora, so an unclosed one is unfiltered
        mail held at rest forever; and a closed corpus carrying ``rows: 0`` is
        how a genuinely quiet night stays distinguishable from a capture stage
        that died."""
        from .. import cos_corpus as corpus_mod

        self._require_host("close the COS capture corpus")
        return corpus_mod.close_run(self.vault, run_id)
    def cos_corpus_reopen(self, run_id: str) -> dict[str, Any]:
        """HOST-ONLY: retract a close that certified ZERO rows.

        Run 68 closed its corpus with ``rows: 0`` after a transient browser
        failure, then recovered and opened three real bodies that could never
        be captured. A zero-row close certifies nothing — no denominator, no
        replay scope, no ledger row — so it alone is retractable. A close
        carrying rows is refused here and always will be."""
        from .. import cos_corpus as corpus_mod

        self._require_host("reopen the COS capture corpus")
        return corpus_mod.reopen_run(self.vault, run_id)
    def cos_ingest_sweep(self, *, downloads_dir: str | Path | None = None,
                         dry_run: bool = False) -> dict[str, Any]:
        """HOST sweeper (v2.1 field bug b): claim VM ingest-manifest lines and
        move exact-filename matches from an explicitly configured dedicated
        host staging dir into ``<vault>/inbox/``. Never defaults to the user's
        shared ``~/Downloads`` directory."""
        from .. import cos as cos_mod

        self._require_host("sweep host downloads into the ingest inbox")
        return cos_mod.ingest_sweep(self.vault, downloads_dir=downloads_dir,
                                    dry_run=dry_run)
    def cos_correct(self, round_: int, msg_key: str, bucket: str, tier: str,
                    *, actor: str = "host-cli") -> dict[str, Any]:
        """HOST-only correction of record (append-only correction_events)."""
        from .. import cos as cos_mod

        self._require_host("record a COS correction")
        return cos_mod.record_correction(self.vault, round_, msg_key, bucket,
                                         tier, actor=actor)
    def cos_evidence_sign(self, **kwargs: Any) -> dict[str, Any]:
        from .. import cos as cos_mod

        self._require_host("sign COS trust-gate evidence")
        return cos_mod.sign_evidence(self.vault, **kwargs)
    def cos_evidence_verify(self, bundle_dir: str | Path) -> dict[str, Any]:
        from .. import cos as cos_mod

        self._require_host("verify COS evidence (resolves the signing key)")
        return cos_mod.verify_evidence(bundle_dir)
    def cos_priority_map(self, *, max_tier: str | None = None) -> dict[str, Any]:
        from .. import cos as cos_mod

        self._require_host("generate the COS priority map")
        return cos_mod.generate_priority_map(self, max_tier=max_tier)
    def cos_report(self) -> dict[str, Any]:
        """HOST-only shadow-mode calibration report (verdicts × corrections)."""
        from .. import cos as cos_mod

        self._require_host("read the COS calibration report")
        return cos_mod.calibration_report(self.vault)
    def cos_hold_add(self, content: str, *, not_before: str,
                     ident: str | None = None) -> dict[str, Any]:
        from .. import cos as cos_mod

        self._require_host("add an auto-capture hold")
        return cos_mod.hold_add(self.vault, content, not_before=not_before,
                                ident=ident)
    def cos_hold_list(self) -> list[dict[str, Any]]:
        from .. import cos as cos_mod

        self._require_host("list auto-capture holds")
        return cos_mod.hold_list(self.vault)
    def cos_hold_cancel(self, ident: str) -> bool:
        from .. import cos as cos_mod

        self._require_host("cancel an auto-capture hold")
        return bool(cos_mod.hold_undo(self.vault, ident, core=self)["undone"])
    def cos_hold_undo(self, ident: str) -> dict[str, Any]:
        """The full undo state machine (held → releasing → capture-pending →
        signed), with the audited-retirement branch available because this
        call has the host core. Demotes the item's category in every branch."""
        from .. import cos as cos_mod

        self._require_host("undo an auto-captured item")
        return cos_mod.hold_undo(self.vault, ident, core=self)
    def cos_hold_release_due(self) -> list[str]:
        from .. import cos as cos_mod

        self._require_host("release due auto-capture holds")
        return cos_mod.hold_release_due(self.vault)
    def cos_spine_record(self, *, event: str, direction: str | None = None,
                         counterparty: str | None = None, text: str | None = None,
                         topic: str | None = None, due: str | None = None,
                         source_ref: str | None = None, note: str | None = None,
                         commitment_id: str | None = None) -> dict[str, Any]:
        """HOST-only manual/host-sourced spine event (e.g. a calendar
        follow-up or drafts-ledger item spotted outside the ingestion
        pipeline). Ingestion-candidate commitments are recorded automatically
        by ``cos_broker_fold`` on owner acceptance — this verb is for the
        other two named sources (calendar follow-ups, the drafts ledger)."""
        from .. import spine as spine_mod

        self._require_host("record a commitment-spine event")
        return spine_mod.record_event(
            self.vault, event=event, direction=direction, counterparty=counterparty,
            text=text, topic=topic, due=due, source_ref=source_ref, note=note,
            commitment_id=commitment_id)
    def cos_spine_radar(self) -> dict[str, Any]:
        from .. import spine as spine_mod

        self._require_host("read the commitment-spine radar")
        return spine_mod.radar(self.vault)
    def cos_spine_render(self, *, now: Any = None) -> dict[str, Any]:
        from .. import spine as spine_mod

        self._require_host("render the commitment-spine summary")
        return spine_mod.render_spine_summary(self.vault, now)
    def cos_grounding_pack(self, *, now: Any = None) -> dict[str, Any]:
        """BAK-01: render the VM-readable grounding pack — Internal-safe
        POINTERS to documents the VM leg's egress ceiling now hides from it
        (owner ruling 2026-08-10). Same host-writes/VM-reads `shared/` lane as
        `cos_spine_render` above; see `spine.render_grounding_pack`."""
        from .. import spine as spine_mod

        self._require_host("render the grounding pack (reads at full tier)")
        return spine_mod.render_grounding_pack(self, now)
    def _engine_feedback_dir(self) -> Path:
        return config.brain_runtime_dir(self.vault) / "engine-feedback"
    def retro(self, *, today: Any = None) -> dict[str, Any]:
        """Retro fold: scan this vault's own maintenance output for engine
        FAILURE SIGNATURES (future dates, absolute-path leakage, duplicate
        findings, future artifacts, hot.md bloat) and write a ready-to-run
        engine-repo prompt into ``.brain/engine-feedback/`` for each. Host-only,
        idempotent (one file per signature per day). Returns the signature ->
        evidence map plus the feedback files written."""
        from .. import retro as rmod
        import datetime as _dt
        import re
        self._require_host("run the retro fold")
        d = today or _dt.date.today()

        hot_path = self._hot_md_path()
        hot_text = hot_path.read_text(encoding="utf-8") if hot_path.exists() else ""
        hot_bytes = hot_path.stat().st_size if hot_path.exists() else 0
        findings = rmod.scan(hot_text, config.brief_dir(self.vault), d, hot_md_bytes=hot_bytes)

        written: list[str] = []
        fb_dir = self._engine_feedback_dir()
        # De-dup on the EVIDENCE FINGERPRINT across both the open queue and
        # resolved/ — not on the dated filename. hot.md is append-only, so the
        # entries proving a signature never disappear; keying the file on the
        # run date re-filed an already-fixed, already-resolved defect under a
        # fresh name every run (measured 2026-07-27). A fingerprint already
        # sitting in resolved/ is a defect the operator has closed: never
        # re-open it. Genuinely new evidence yields a new fingerprint and files.
        seen = {
            m.group(1)
            for p in list(fb_dir.glob("*.md")) + list((fb_dir / "resolved").glob("*.md"))
            for m in [re.search(r"-([0-9a-f]{12})\.md$", p.name)] if m
        }
        for signature, evidence in findings.items():
            fp = rmod.evidence_fingerprint(signature, evidence)
            if fp in seen:
                continue
            slug, md = rmod.render_engine_feedback(signature, evidence, d)
            fpath = fb_dir / f"{slug}.md"
            if not fpath.exists():
                fb_dir.mkdir(parents=True, exist_ok=True)
                fpath.write_text(md, encoding="utf-8")
                written.append(fpath.name)
                seen.add(fp)
        return {"date": d.isoformat(), "findings": findings, "feedback_written": written}
    def pending_engine_feedback(self) -> list[str]:
        """Filenames of engine-feedback prompts waiting to be fired at the repo
        (surfaced by the SessionStart hook)."""
        fb_dir = self._engine_feedback_dir()
        return sorted(f.name for f in fb_dir.glob("*.md")) if fb_dir.is_dir() else []
