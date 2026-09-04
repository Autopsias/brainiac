"""Health assessment methods for BrainCore."""
from __future__ import annotations

from ._shared import (
    Any,
    _audit_status_summary,
    config,
    source_repo_root,
)


def _empty_chain_item(core: Any, maint: Any) -> "dict[str, Any] | None":
    """The action-required item for an EMPTY audit chain, or ``None`` when this
    vault has genuinely never signed anything.

    M-7 (2026-09-02). An empty chain is two very different vaults wearing the
    same face: one that has never signed a note, and one whose log was wiped.
    ``verify()`` cannot tell them apart — zero entries, zero errors, both — so
    ``health()`` and ``integrity()`` listed ``"empty"`` beside ``"ok"`` and
    called the wipe green. Those two are the surfaces that matter: they back
    ``brain health`` / ``brain integrity``, which is what ``brain maintain``
    and the nightly fold actually run, so a wiped log produced a fully green
    unattended night while only an interactive exit code said otherwise.

    The distinguisher is a TRACE of past signing, never a guess: the log FILE
    exists. ``audit_chain._append_record`` is the only thing that creates it
    (``O_CREAT``), and every read path returns ``[]`` for an absent one, so its
    presence means at least one signed append landed here — and a log truncated
    to zero bytes still has it.

    Known negative: a brand-new vault has no log file and stays silent. That
    includes the freshly SEEDED one — ``init_samples`` writes its four sample
    notes with ``write_text``, never through the signing path.

    RESIDUAL, stated rather than hidden: a wipe that DELETES the log on a vault
    with no off-host anchor leaves no trace for this check to find. That is
    exactly the gap ``integrity()``'s "no off-host anchor configured" item
    already tells the operator to close.
    """
    log = getattr(core.audit, "log_path", None) if core.audit else None
    if not (log and log.exists()):
        return None
    item = maint.action_required_item(
        "audit chain is EMPTY — no signed notes, but this vault has signed before",
        "the log file is present and holds no entries; a wiped log and a "
        "never-signed vault verify identically, so this cannot be read as a pass",
        "treat the chain as compromised: re-establish it from the last "
        "known-good copy and check the off-host anchor. "
        "`brain verify-audit --allow-empty` is for a vault that has genuinely "
        "never signed a note, not for this one",
        str(log))
    # Carried forward from s04/s10b: without a notify_key this item reaches
    # `run.action_required` (folds/weekly.py) but never `degradation_findings`
    # (maintenance_notify.py), so it never lands in `notify-sent/current.json`
    # and `brain alerts` never sees it — a finding without a channel dies in
    # the log, the same class the quarantine and declassification banners
    # were fixed for.
    item["notify_key"] = "audit-chain-empty"
    return item


class _CoreHealthMixin:
    """Health assessment methods for BrainCore."""

    def check(self, *, dry_run: bool = False) -> dict[str, Any]:
        """daily-check fold: index reconcile + drain drafts + freshness status
        (task-disposition.md row 1). ``dry_run`` skips the mutation and reports
        status only — still a real read against the live index."""
        from .. import maintenance as maint

        self._require_host("run the check ritual")
        auto_fixed: list[dict[str, Any]] = []
        action_required: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []

        sync_res: dict[str, Any] | None = None
        if not dry_run:
            sync_res = self.sync(drain=True, publish=False)
            added = sync_res.get("added", 0)
            updated = sync_res.get("updated", 0)
            deleted = sync_res.get("deleted", 0)
            if added or updated or deleted:
                auto_fixed.append(maint.auto_fixed_item(
                    "sync", str(self.vault),
                    f"index reconciled +{added} ~{updated} -{deleted}"))
            drain = sync_res.get("drain", {}) or {}
            if drain.get("promoted"):
                auto_fixed.append(maint.auto_fixed_item(
                    "drain", str(self.capture_inbox_dir()),
                    f"drained {drain['promoted']} pending capture(s)"))
            details = drain.get("details", {})
            skipped_list = details.get("skipped", []) if isinstance(details, dict) else []
            for skip in skipped_list:
                reason = skip.get("reason", "")
                # Attribute by SOURCE, not by matching words in the reason: an
                # approved-queue item that is skipped for ANY cause (duplicate
                # id, bad frontmatter, a refusal) does not live in capture-inbox,
                # and printing a path that does not exist sends the operator to
                # the wrong directory.
                if skip.get("source") == "approved-queue":
                    from .. import cos as _cos_q
                    draft_path = str(_cos_q.approved_queue_dir(self.vault)
                                     / skip.get("draft", ""))
                    refused = "approved-queue refusal" in reason
                    action_required.append(maint.action_required_item(
                        (f"owner-approved candidate {skip.get('draft')} was REFUSED "
                         f"at the signing gate (not signed)" if refused else
                         f"owner-approved candidate {skip.get('draft')} could not "
                         f"be signed"),
                        reason,
                        ("inspect the quarantined *.refused copy and the "
                         "approved-queue-refusal defect row; re-propose if the "
                         "content is still wanted" if refused else
                         "resolve the cause above — the item stays queued and is "
                         "retried on every drain until it is"),
                        draft_path))
                    continue
                draft_path = str(self.capture_inbox_dir() / skip.get("draft", ""))
                if "no-signing-key" in reason:
                    blocked.append(maint.blocked_item(
                        f"capture draft {skip.get('draft')} could not be drained",
                        "no audit signing key resolved",
                        "signing key configured (Keychain/env), then re-run check"))
                else:
                    action_required.append(maint.action_required_item(
                        f"capture draft {skip.get('draft')} could not be drained",
                        reason or "unrecognised draft frontmatter",
                        "fix the draft's frontmatter, then re-run check",
                        draft_path))

        status_res = self.status()
        return {
            "ritual": "check", "dry_run": dry_run,
            "sync": sync_res, "status": status_res,
            "outcomes": maint.build_outcomes(auto_fixed, action_required, blocked),
        }
    def health(self) -> dict[str, Any]:
        """health fold: index/snapshot status + audit-chain verify + a
        substrate self-test probe (task-disposition.md row 2). Entirely
        READ-ONLY — safe to run under a caller's --dry-run posture too."""
        from .. import maintenance as maint

        self._require_host("run the health ritual")
        action_required: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []

        status_res = self.status()

        audit_res: dict[str, Any] | None = None
        try:
            audit_res = self.verify_audit()
            status = audit_res.get("status")
            if status == "empty":
                empty_item = _empty_chain_item(self, maint)
                if empty_item is not None:
                    action_required.append(empty_item)
            elif status != "ok":
                action_required.append(maint.action_required_item(
                    _audit_status_summary(audit_res),
                    "chain tamper/break needs human judgment, never auto-repaired",
                    "inspect the chain errors; re-link from the last-good entry "
                    "(content drift: `brain verify-audit --check-content --json`)",
                    str(self.audit.log_path) if self.audit else "audit chain"))
        except Exception as exc:
            blocked.append(maint.blocked_item(
                "could not verify the audit chain",
                f"{type(exc).__name__}: {exc}",
                "signing key configured (Keychain/env), then re-run health"))

        selftest: dict[str, Any] = {"probe_ok": False}
        live = status_res.get("live_embedder", {})
        try:
            hits = self.hybrid_search("brain", k=1)
            ix = status_res.get("index", {})
            selftest = {
                "probe_ok": True, "result_count": len(hits),
                "vector_backend": ix.get("vector_backend"),
                "embed_model": ix.get("embed_model"),
                # LIVE embedder actually in use (S11) — distinct from the index's
                # recorded embed_model metadata above.
                "live_embedder": live.get("model_id"),
                "hash_fallback": bool(live.get("is_hash_fallback")),
            }
        except Exception as exc:
            blocked.append(maint.blocked_item(
                "retrieval self-test probe raised",
                f"{type(exc).__name__}: {exc}",
                "investigate the embedder/vector-backend, then re-run health"))
        # A live HashEmbedder on an index built with a real model is a silent
        # semantic downgrade — surface it as ACTION REQUIRED, not a pass.
        if live.get("is_hash_fallback") and not live.get("matches_index_metadata"):
            action_required.append(maint.action_required_item(
                "live embedder is the non-semantic HashEmbedder but the index was "
                f"built with {status_res.get('index', {}).get('embed_model')!r}",
                "retrieval quality is effectively random on this install "
                "(onnxruntime/tokenizers missing or the bge-m3-int8 model absent)",
                "install the 'corporate' extras (onnxruntime + tokenizers) or the "
                "bundled model; set BRAIN_REQUIRE_REAL_EMBEDDER=1 to fail closed",
                "brain status --json (live_embedder block)"))

        return {
            "ritual": "health", "status": status_res, "audit": audit_res,
            "selftest": selftest,
            "outcomes": maint.build_outcomes([], action_required, blocked),
        }
    def _framework_sync_finding(self) -> dict[str, Any] | None:
        """HYG-02 (ADR-0003 Ruling 5): the Monday health branch also runs the
        framework-sync drift audit (canonical .claude/skills/ vs the
        .agents/skills/ + plugins/ mirrors, plus CLAUDE.md's @AGENTS.md
        import) — reported as a health finding, NEVER auto-fixed. Best-effort
        and silent when not applicable: ``tools/framework_sync.py`` is a
        dev-only script that lives in the profile-a-brain source checkout,
        not the installed package, so a generic installed vault (no sibling
        ``tools/`` tree) simply has nothing to compare and skips."""
        from .. import maintenance as maint

        repo_root = source_repo_root()
        if repo_root is None:
            return None
        fsync_path = repo_root / "tools" / "framework_sync.py"
        if not fsync_path.is_file():
            return None
        import sys as _sys
        tools_dir = str(repo_root / "tools")
        inserted = tools_dir not in _sys.path
        if inserted:
            _sys.path.insert(0, tools_dir)
        try:
            import framework_sync as fsync
            report = fsync.audit()
        except Exception as exc:
            return maint.action_required_item(
                f"framework-sync drift audit raised: {type(exc).__name__}: {exc}",
                "could not compare .claude/skills/ against its mirrors",
                "inspect tools/framework_sync.py, then re-run health",
                str(fsync_path))
        finally:
            if inserted and tools_dir in _sys.path:
                _sys.path.remove(tools_dir)
        return maint.framework_sync_finding(report)
    def curate(
        self, *, dry_run: bool = False, k: int = 50, today: Any = None,
    ) -> dict[str, Any]:
        """curation fold (task-disposition.md row 4, extended by AUT-02): the
        refresh-index sub-step folds to ``sync``; unclassified-notes lint,
        stale-wikilink-target detection, and an age x centrality revisit
        sample now run directly against the brain index (ADR-0003 Ruling 5 —
        this SUPERSEDES the curation skill's previously-documented "no brain
        equivalent, G3 not shipped" framing for those two checks). Orphan/
        contradiction/callout lint stay vault-structure overlay tooling with
        NO brain equivalent (G4 RETIRE) — those still route through
        ``.claude/skills/curation``. UNFILTERED findings — the CLI
        egress-gates every one before surfacing."""
        from .. import maintenance as maint

        self._require_host("run the curate ritual")
        auto_fixed: list[dict[str, Any]] = []

        sync_res: dict[str, Any] | None = None
        if not dry_run:
            sync_res = self.sync(drain=False)
            added = sync_res.get("added", 0)
            updated = sync_res.get("updated", 0)
            deleted = sync_res.get("deleted", 0)
            if added or updated or deleted:
                auto_fixed.append(maint.auto_fixed_item(
                    "sync", str(self.vault),
                    f"refresh-index +{added} ~{updated} -{deleted}"))

        unclassified = self.index.unclassified_notes(k=k)
        stale_links = self.index.stale_wikilink_targets()
        revisit_sample = self.index.revisit_sample(today=today, k=10)
        return {
            "ritual": "curate", "dry_run": dry_run, "sync": sync_res,
            "unclassified_notes": unclassified,  # UNFILTERED
            "stale_links": stale_links,          # UNFILTERED
            "revisit_sample": revisit_sample,    # UNFILTERED
            "overlay_only_skipped": {
                "orphans": "vault-structure overlay, no brain equivalent (RETIRE)",
                "contradictions": "vault-structure overlay, no brain equivalent (RETIRE)",
                "callouts": "vault-structure overlay, no brain equivalent (RETIRE)",
            },
            "auto_fixed": auto_fixed,
        }
    def integrity(
        self, *, min_score: float = 0.95, k: int = 5, scan_injection: bool = True,
    ) -> dict[str, Any]:
        """integrity-scan fold (task-disposition.md row 3): audit-chain verify
        + a corpus-wide near-dup scan directly over the brain vector backend
        (brain-cli-gaps.md G1 — no SC/MCP round-trip). READ-ONLY. UNFILTERED
        ``near_dup_pairs`` — the CLI egress-gates BOTH members of every pair
        before surfacing (G1's explicit requirement)."""
        from .. import maintenance as maint

        self._require_host("run the integrity ritual")
        blocked: list[dict[str, Any]] = []

        audit_res: dict[str, Any] | None = None
        try:
            audit_res = self.verify_audit()
        except Exception as exc:
            blocked.append(maint.blocked_item(
                "could not verify the audit chain",
                f"{type(exc).__name__}: {exc}",
                "signing key configured (Keychain/env), then re-run integrity"))

        audit_issue: dict[str, Any] | None = None
        _status = audit_res.get("status") if audit_res else None
        if _status == "empty":
            audit_issue = _empty_chain_item(self, maint)
        elif audit_res and _status != "ok":
            audit_issue = maint.action_required_item(
                _audit_status_summary(audit_res),
                "chain tamper/break needs human judgment, never auto-repaired",
                "inspect the chain errors; re-link from the last-good entry "
                "(content drift: `brain verify-audit --check-content --json`)",
                str(self.audit.log_path) if self.audit else "audit chain")

        # M-2: `verify()` above only checks linkage + signatures over the
        # entries PRESENT in the log — deleting the tail (never re-signing)
        # still verifies "ok". Folding the off-host anchor check in here is
        # what actually detects a truncated tail (chain_shorter_than_anchor).
        # M-7: its OWN slot, not `audit_issue`. It used to be written only
        # `if audit_issue is None`, so the moment the chain had any other
        # finding — the case where a truncated tail matters MOST — the one
        # warning saying nothing could detect a truncated tail disappeared.
        anchor_issue: dict[str, Any] | None = None
        adir = config.anchor_dir()
        if adir is None:
            anchor_issue = maint.action_required_item(
                "no off-host anchor configured (BRAIN_ANCHOR_DIR unset)",
                "verify() alone gives NO tail-truncation guarantee — "
                "deleting recent audit-log lines still verifies ok",
                "run `brain anchor --anchor-dir <off-host-dir>` on a "
                "schedule, then set BRAIN_ANCHOR_DIR so integrity/maintain "
                "can check it",
                str(self.audit.log_path) if self.audit else "audit chain")
        else:
            try:
                anchor_res = self.verify_anchor(adir)
            except Exception as exc:
                blocked.append(maint.blocked_item(
                    "could not verify the off-host anchor",
                    f"{type(exc).__name__}: {exc}",
                    "check BRAIN_ANCHOR_DIR is reachable, then re-run integrity"))
            else:
                if anchor_res.get("status") == "divergence":
                    anchor_issue = maint.action_required_item(
                        f"audit chain diverges from off-host anchor "
                        f"({len(anchor_res.get('divergences', []))} divergence(s))",
                        "tail truncation or a silent rewrite is possible — "
                        "human judgment, never auto-repaired",
                        "inspect anchor divergences; treat the chain as "
                        "compromised from the first divergent entry_count",
                        anchor_res.get("anchor_log", str(adir)))

        try:
            pairs = self.index.near_dup(min_score=min_score, k=k)
        except Exception as exc:
            pairs = []
            blocked.append(maint.blocked_item(
                "near-dup scan raised",
                f"{type(exc).__name__}: {exc}",
                "investigate the embedder/vector-backend, then re-run integrity"))

        # SEC-05 corpus re-scan and the SEC-06 bulk-read alarm. Both lived ONLY
        # in the `brain integrity` CLI body until 2026-09-01, so the Tuesday
        # fold — the one caller that runs with no human present — fired neither
        # of the two detectors built for indirect prompt injection. Moving them
        # here is what arms them. UNFILTERED like `near_dup_pairs`: a finding
        # NAMES a note, so every SURFACING caller egress-gates it. `cover` is
        # the V11 coverage census — counts only, so IT is not gated.
        rows, reads, cover = self._injection_and_reads(scan_injection, blocked)

        return {
            "ritual": "integrity", "min_score": min_score,
            "audit": audit_res, "audit_issue": audit_issue,
            "anchor_issue": anchor_issue,
            "near_dup_pairs": pairs,  # UNFILTERED
            "injection_rows": rows,  # UNFILTERED
            "injection_coverage": cover,  # counts only — NOT gated (V11)
            "read_log": reads,
            "blocked": blocked,
        }
    def _injection_and_reads(
        self, scan_injection: bool, blocked: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, int] | None]:
        """The two prompt-injection detectors, split out to keep `integrity`
        inside the function-length ratchet. Appends to ``blocked`` in place:
        a scan that raises is a reported gap, never a silent clean sweep.

        The third return is the CONCEALMENT-SCAN COVERAGE census — how many
        notes read `full`/`off`/`incomplete`/`unknown`/`absent`. Rows alone
        could never report a note stamped `off` or `unknown` (its hidden
        count is 0 by construction, so the `clean` skip drops it), which is
        what made the key write-only (V11, 2026-09-02)."""
        from .. import injection_scan as _isc
        from .. import maintenance as maint
        from .. import read_log as _rl

        rows: list[dict[str, Any]] = []
        coverage: dict[str, int] | None = None
        if scan_injection:
            try:
                rows = _isc.scan_corpus(self.vault)
                coverage = _isc.scan_coverage(self.vault)
            except Exception as exc:
                blocked.append(maint.blocked_item(
                    "concealed-instruction scan raised",
                    f"{type(exc).__name__}: {exc}",
                    "investigate brain.injection_scan, then re-run integrity"))
        try:
            reads = _rl.status(self.vault)
        except Exception:
            reads = {"available": False, "reason": "read-log unreadable"}
        return rows, reads, coverage

    def promote_scan(self, *, k: int = 50) -> dict[str, Any]:
        """promotion-scan fold (task-disposition.md row 5 — ON-INVOKE triage;
        promotion itself stays a P-10 human gate). Candidates: ``raw/`` zone
        sources not yet promoted into a typed ``brain/`` note. UNFILTERED — the
        CLI egress-gates the candidate list before surfacing."""
        self._require_host("run the promote-scan ritual")
        candidates = self.index.bases_query({"zone": "raw"}, k=k)
        return {
            "ritual": "promote-scan",
            "candidates": candidates,  # UNFILTERED
            "pending_drafts": self._count_pending_drafts(),
        }
