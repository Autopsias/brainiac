"""Stage external intake before the nightly sync."""

from __future__ import annotations

import hashlib as _hashlib
import json as _json

from .context import MaintenanceRun
from .. import config, maintenance


class IntakeFoldsMixin:
    """Provide pre-sync maintenance intake folds."""

    def future_artifact_fold(self, run: MaintenanceRun) -> None:
        """Remove future-dated derived brief artifacts before regeneration."""
        reaped = maintenance.reap_future_dated_artifacts(
            config.brief_dir(self.vault), run.date
        )
        if reaped:
            run.auto_fixed.append(
                maintenance.auto_fixed_item(
                    "reap-future-artifacts",
                    "brief/",
                    f"removed {len(reaped)} future-dated brief/digest file(s): "
                    f"{', '.join(reaped)}",
                )
            )

    def workspace_sweep_fold(self, run: MaintenanceRun) -> None:
        """Sweep settled workspace files into the ingestion drop zone."""
        sweep_dirs, sweep_age = maintenance.workspace_sweep_config()
        if not sweep_dirs:
            return
        try:
            result = maintenance.sweep_workspace(
                sweep_dirs, self.vault / "inbox", sweep_age
            )
            run.results["workspace_sweep"] = result
            if result["swept"]:
                run.auto_fixed.append(
                    maintenance.auto_fixed_item(
                        "workspace-sweep",
                        str(self.vault / "inbox"),
                        f"swept {len(result['swept'])} settled workspace file(s) "
                        f"into inbox/ (age>{sweep_age}d)",
                    )
                )
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"workspace sweep failed: {exc}",
                    "filesystem",
                    "next maintain run",
                )
            )

    def provision_drain_fold(self, run: MaintenanceRun) -> None:
        """Drain pending new-vault provision requests written by a Cowork session.

        PRV-10 (VM-request → host-drain, owner ruling 2026-08-16: automatic,
        loudly reported). Rides the hourly daily branch instead of a new
        scheduled task (AGENTS.md §6). Cheap no-op scan when no request is
        pending."""
        try:
            from .. import provision as _provision
            _provision.maintain_fold(
                run.results, run.auto_fixed, run.action_required)
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"provision drain failed: {exc}",
                    "workspace registry / filesystem",
                    "next maintain run",
                )
            )

    def cos_ingest_sweep_fold(self, run: MaintenanceRun) -> None:
        """Quarantine manifest-named downloads for the current owner batch."""
        try:
            result = self.cos_ingest_sweep()
            run.results["cos_ingest_sweep"] = result
            if result.get("moved"):
                run.auto_fixed.append(
                    maintenance.auto_fixed_item(
                        "cos-ingest-sweep",
                        str(self.vault),
                        f"quarantined {len(result['moved'])} manifest-named "
                        "download(s) for an owner verdict",
                    )
                )
            self._cos_staging_divergence(run, result)
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"COS ingest sweep failed: {exc}",
                    "downloads dir / cos ops dir",
                    "next maintain run",
                )
            )
        self._cos_attachment_join_record(run)
        self._cos_attachment_withheld(run)
        self._cos_unreadable_threads(run)

    def _cos_staging_divergence(self, run: MaintenanceRun,
                                result: dict) -> None:
        """Surface a sweep pointed at a directory the fetch lane does not fill.

        THE QUIET ZERO IS THE FINDING. For four days on the reference host the
        sweep read a configured, EMPTY staging directory and reported an
        ordinary `moved: []` — while 79 fetched files aged out of the six-hour
        recency window in the directory the fetch lane actually writes. Nothing
        in the maintain report distinguished that from a night with no
        attachments. `action_required`, not `blocked`: the fix is a launchd
        edit the OWNER makes, and the engine may not write one.
        """
        finding = result.get("misconfigured")
        if not finding:
            return
        item = maintenance.action_required_item(
                f"the COS attachment sweep is reading "
                f"{finding['configured']}, which holds none of the "
                f"{finding['manifest_names_unmatched']} file(s) this vault's "
                f"ingest manifest names — "
                f"{finding['manifest_names_found_in_default']} of them are in "
                f"{finding['engine_default']}",
                "the job that fetches and the job that sweeps name different "
                "staging directories, so fetched attachments are never "
                "claimed and age out of the recency window",
                f"repoint BRAIN_COS_DOWNLOADS_DIR on the job that runs `brain "
                f"maintain` to {finding['engine_default']}, or unset it so the "
                "engine default applies",
                str(finding.get("examples") or []),
        )
        # WITHOUT THIS IT NEVER REACHES THE OWNER (adversarial review,
        # 2026-09-05). `maintenance_notify` only forwards an `action_required`
        # item that carries its own stable dedup key: "Without one an item
        # stays in the maintain result and never reaches `brain alerts`."
        # The finding this fold exists to raise is the one that hid a staging
        # divergence for four days, so a report-only surface nobody reads is
        # the same silence with an extra file in it. Keyed by the CONFIGURED
        # directory so a repoint mints a new alert and a persisting fault
        # de-duplicates rather than nagging nightly.
        # THE DIRECTORY GOES IN AS A DIGEST, NOT AS A PATH (2026-09-06). This
        # line interpolated the raw path, and `alerts._FINDING_KEY_RE` accepts
        # only `[a-z0-9:_-]{1,64}` — a closed vocabulary that exists because
        # this feed sits on the VirtioFS mount the Cowork VM can write, and is
        # the one thing standing between a forged feed and attacker-authored
        # text in the host SessionStart context. A key carrying `/` and capital
        # letters failed it, so the finding was WITHHELD from every rendered
        # surface and the owner saw only an anonymous "1 finding ... was
        # withheld" line. Measured on the reference host that day: the withheld
        # finding was this very divergence, correctly detected, naming 56
        # fetched files the sweep could not see — the exact four-day silence
        # this fold was written to end, hidden by its own key. A digest keeps
        # every property the comment above claims (same directory, same key;
        # repoint, new key) and the path stays in the human text, which is
        # rendered through its own escaping. Same recipe as the sibling
        # `_cos_attachment_withheld` below.
        digest = _hashlib.sha256(
            str(finding["configured"]).encode("utf-8")).hexdigest()[:12]
        item["notify_key"] = f"cos-staging-divergence:{digest}"
        run.action_required.append(item)

    def _cos_attachment_withheld(self, run: MaintenanceRun) -> None:
        """Say WHICH threads the file gate is holding, and why (ATT-03).

        THE SENTENCE WAS COMPUTED AND THROWN AWAY (adversarial review,
        2026-09-05). `attachment_lane_withheld` builds a full explanation per
        thread — which lines, in which state, how many attachments
        unaccounted for — and its one production caller reduced it with
        `set()`, keeping the ids and dropping every word. So a thread the gate
        held looked exactly like a thread nobody judged: not chipped, no
        reason, on every surface the owner reads. Measured on the reference
        host, that is ~9 threads a run going quiet the first night this ships.

        The NEWEST ledger, not tonight's: the gate is evaluated per run and
        the newest ingestion ledger is the one whose answer is current. Read
        from `run_ops_dir` because that is where the run wrote it; no mailbox
        call, no signing, no index. NEWEST BY RUN NUMBER, not by filename
        (adversarial review pass 2, 2026-09-05): run ids are `<date>-run<N>`
        with N unpadded, so a lexical sort puts `run99` after `run100` and
        picks the wrong ledger at a same-day rollover. This host is at run 260
        and runs several passes a day.

        Reported, never fatal — like its sibling above, this is a description
        of a decision another function already made, so failing to describe it
        must not fail the maintain run.
        """
        try:
            from .. import cos                                   # noqa: PLC0415

            ops = cos.run_ops_dir(self.vault)
            ledgers = sorted(ops.glob("_cos_ingestion_ledger_*.jsonl"),
                             key=self._ledger_order)
            if not ledgers:
                return
            latest = ledgers[-1]
            run_id = latest.stem[len("_cos_ingestion_ledger_"):]
            rows, seen = [], set()
            for line in latest.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = _json.loads(line)
                cid = str(row.get("conversation_id") or "")
                if cid and cid not in seen:
                    seen.add(cid)
                    rows.append(row)
            withheld = cos.attachment_lane_withheld(self.vault, run_id, rows)
            run.results["cos_attachment_withheld"] = {
                "run": run_id, "threads": len(withheld), "why": withheld}
            self._withheld_alert(run, run_id, withheld)
        except Exception as exc:                                 # noqa: BLE001
            run.results["cos_attachment_withheld"] = {
                "error": f"{type(exc).__name__}: {exc}"[:300]}

    @staticmethod
    def _ledger_order(path) -> tuple[str, int]:
        """Sort key for `_cos_ingestion_ledger_<date>-run<N>.jsonl`: the date
        as text, then the run number as an INT. A missing or unparseable run
        number sorts first, which keeps a malformed name from winning."""
        stem = path.stem[len("_cos_ingestion_ledger_"):]
        date, _, tail = stem.partition("-run")
        return (date, int(tail) if tail.isdigit() else -1)

    def _withheld_alert(self, run: MaintenanceRun, run_id: str,
                        withheld: dict[str, str]) -> None:
        """Put the held threads where the owner actually looks.

        THE MAINTAIN RESULT IS NOT A SURFACE HE READS (adversarial review pass
        2, 2026-09-05). `maintenance_notify` forwards only an `action_required`
        item carrying its own dedup key; a bare `run.results` entry stays in
        the maintain output and never reaches `brain alerts` or the exceptions
        page. That matters for THIS finding above all others, because the whole
        point of the change is that threads stop being chipped and stop being
        archived. The owner's visible symptom is an inbox that grows, and on an
        unattended nightly the sentence explaining it would live only in a
        launchd log.

        KEYED BY THE SET OF THREADS, not by the run. Keying by run id mints a
        new alert every night for a condition that has not changed; keying by a
        constant announces the first hold and then stays silent while the set
        turns over completely. The digest of the sorted conversation ids says
        "these threads are held" — a persisting hold de-duplicates, and a
        thread joining or leaving it is a new thing to say.
        """
        if not withheld:
            return
        ids = sorted(withheld)
        digest = _hashlib.sha256("\n".join(ids).encode()).hexdigest()[:12]
        item = maintenance.action_required_item(
            f"{len(ids)} thread(s) in {run_id} carry an attachment the vault "
            f"does not have, so they are not chipped and will not be archived",
            "the file lane (ATT-03) holds a thread whose bytes never reached a "
            "signed note; the text of these threads may be in the vault, their "
            "files are not",
            "run `brain maintain` and read `cos_attachment_withheld` for the "
            "per-thread reason, then re-run the fetch for the named files",
            "; ".join(f"{cid}: {why}" for cid, why in
                      sorted(withheld.items())[:3]),
        )
        item["notify_key"] = f"cos-attachment-withheld:{digest}"
        run.action_required.append(item)

    #: The porter opened the thread (or tried) and the mail server would not
    #: give up a usable body. These are TERMINAL: no later night reads them,
    #: because nothing about the thread will change. `server-returned-no-body`
    #: and `never-category` are s09's own terminal reasons; the other two come
    #: from the body lane. `no-substance` is deliberately NOT here — that body
    #: WAS read and simply said nothing, which is a judgment, not a refusal.
    UNREADABLE_BODY_REASONS = ("rights-protected-message",
                               "server-returned-no-body",
                               "rest-read-returned-shell",
                               "never-category")

    def _cos_unreadable_threads(self, run: MaintenanceRun) -> None:
        """The threads the porter can NEVER read — owner ruling 2026-09-06.

        Every auto-archive lane refuses a thread whose body came back
        unusable, and it is right to: `unreadable_body_refusal` records that a
        blanked body makes every content screen read False, so accepting one
        would archive on the ABSENCE of evidence rather than evidence of
        absence. The consequence is that such a thread is never archived,
        never drafted and never decided — it sits in the Inbox for good,
        indistinguishable from a thread nobody has got to yet.

        Measured on `2026-09-06-run267`: **12 of 60 rows**, one night in five,
        split `server-returned-no-body` 5, `rights-protected-message` 3,
        `never-category` 3, `rest-read-returned-shell` 1.

        The owner ruled these get their OWN list he clears by hand — not
        archive-by-default (the porter has not read them, so it cannot say
        there is no action) and not silent holding (which is what they do
        today). This fold is that list.

        Reported, never fatal — like its siblings above, it describes a
        decision another function already made, so failing to describe it must
        not fail the maintain run.
        """
        try:
            from .. import cos                                   # noqa: PLC0415

            ops = cos.run_ops_dir(self.vault)
            ledgers = sorted(ops.glob("_cos_ingestion_ledger_*.jsonl"),
                             key=self._ledger_order)
            if not ledgers:
                return
            latest = ledgers[-1]
            run_id = latest.stem[len("_cos_ingestion_ledger_"):]
            stuck, seen = {}, set()
            for line in latest.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = _json.loads(line)
                cid = str(row.get("conversation_id") or "")
                reason = row.get("held_reason")
                if cid and cid not in seen and reason in self.UNREADABLE_BODY_REASONS:
                    seen.add(cid)
                    stuck[cid] = reason
            run.results["cos_unreadable_threads"] = {
                "run": run_id, "threads": len(stuck), "why": stuck}
            self._unreadable_alert(run, run_id, stuck)
        except Exception as exc:                                 # noqa: BLE001
            run.results["cos_unreadable_threads"] = {
                "error": f"{type(exc).__name__}: {exc}"[:300]}

    def _unreadable_alert(self, run: MaintenanceRun, run_id: str,
                          stuck: dict[str, str]) -> None:
        """Same surface and the same keying rule as `_withheld_alert`.

        KEYED BY THE SET, not by the run: a thread that cannot be read today
        cannot be read tomorrow either, so keying by run id would mint a fresh
        alert every night for a list that never changes. The digest of the
        sorted conversation ids says "these threads are stuck" — a persisting
        set de-duplicates, and a thread joining or leaving it is a new thing
        to say.
        """
        if not stuck:
            return
        ids = sorted(stuck)
        digest = _hashlib.sha256("\n".join(ids).encode()).hexdigest()[:12]
        item = maintenance.action_required_item(
            f"{len(ids)} thread(s) in {run_id} have a body the porter can "
            f"never read, so they will never be archived, drafted or decided "
            f"and will sit in the Inbox until you clear them by hand",
            "every auto-archive lane refuses an unreadable body on purpose — a "
            "blanked body makes every content screen read False, so archiving "
            "one would act on the absence of evidence rather than evidence of "
            "absence. These threads are terminal, not pending",
            "run `brain maintain` and read `cos_unreadable_threads` for the "
            "per-thread reason, then archive or answer each one in Outlook "
            "yourself",
            "; ".join(f"{cid}: {why}" for cid, why in
                      sorted(stuck.items())[:3]),
        )
        item["notify_key"] = f"cos-unreadable-threads:{digest}"
        run.action_required.append(item)

    def _cos_attachment_join_record(self, run: MaintenanceRun) -> None:
        """Record the BYTES-JOIN claim for every run still in reach (ATT-03).

        ON A WINDOW, NOT ON THIS RUN, and that is the whole reason it can sit
        here. The chain takes days to complete: the fetch stages the bytes, the
        sweep quarantines them, the owner answers his batch, and only then does
        an ingest drain sign the note. So the run whose manifest line started a
        join is almost never the run that can see it finish, and
        `record_attachment_joins` re-walks every run still in reach instead of
        asking about tonight.

        That also bounds what this placement costs. `sync_reconcile_fold` —
        which drains `vault/inbox/` and signs — runs AFTER this, so a note
        signed by THIS pass is not in the file until the NEXT one. `brain
        maintain` fires hourly, the row is idempotent on
        (manifest line, content hash), and nothing reads the file to decide
        anything, so an hour of lag in a convenience record is not worth
        reordering a fold block around.

        Reported, never fatal: this is EVIDENCE that a join happened, not the
        authority for it. Every consumer recomputes the chain
        (`attachment_lane_context`), so a failure to write the record costs a
        reader convenience and costs the mark nothing.
        """
        try:
            from .. import cos                                   # noqa: PLC0415
            result = cos.record_attachment_joins(self.vault)
            run.results["cos_attachment_joins"] = result
        except Exception as exc:                                 # noqa: BLE001
            run.results["cos_attachment_joins"] = {
                "error": f"{type(exc).__name__}: {exc}"[:300]}
