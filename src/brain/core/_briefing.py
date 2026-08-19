"""Briefing render methods for BrainCore."""
from __future__ import annotations

from ._shared import (
    Any,
    classification,
    config,
    frontmatter,
    safe_slug,
    source_repo_root,
)


class _CoreBriefingMixin:
    """Briefing render methods for BrainCore."""

    def capture(
        self,
        content: str,
        *,
        note_id: str | None = None,
        note_type: str | None = None,
        classification: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """Unified capture verb (UX-01).

        HOST path: enforce frontmatter → write_note (sign + audit) → incremental
                   sync → note immediately retrievable.
        VM path:   enforce frontmatter → draft_capture (capture-inbox/, unsigned,
                   unindexed) → host drain-on-invoke picks it up on the next run.

        No signing key is ever touched on the VM path. The VM drops an untrusted
        draft; the host validates, signs, and indexes it on drain-on-invoke.
        """
        from .. import capture as cap_mod

        override: dict[str, Any] = {}
        if note_id:
            override["id"] = note_id
        if note_type:
            override["type"] = note_type
        if classification:
            override["classification"] = classification

        enforced = cap_mod.enforce(content, override=override or None)

        if self.role == config.ROLE_HOST:
            meta, _body = frontmatter.parse_text(enforced)
            nid = safe_slug(meta.get("id", "capture"))  # same C-1/C-2 trust boundary
            ntype = str(meta.get("type", "note"))
            if ntype == "source":
                rel, subtree = f"raw/{nid}.md", "raw"
            else:
                rel, subtree = f"brain/resources/{nid}.md", "brain/resources"
            write_res = self.write_note(rel, enforced, reason=reason or f"capture {nid}",
                                        subtree=subtree)
            sync_res = self.sync(drain=False)  # note already written; just reconcile
            return {
                "id": nid,
                "path": write_res["written"],
                "signed": True,
                "indexed": True,
                "role": "host",
                "sync": {
                    "added": sync_res.get("added", 0),
                    "updated": sync_res.get("updated", 0),
                },
            }
        else:
            res = self.draft_capture(enforced, ident=None, is_source=False)
            return {
                "id": res["id"],
                "draft": res["draft"],
                "signed": False,
                "indexed": False,
                "role": "vm",
                "note": "draft in capture-inbox/; host drain-on-invoke will sign + index",
            }
    def brief(
        self, *, max_recent: int = 5, drain: bool = True,
        max_tier: str = classification.DEFAULT_MAX_TIER,
    ) -> dict[str, Any]:
        """Generate the morning brief (UX-02).

        Drains pending captures first (HOST only) — making this the guaranteed
        daily drain FLOOR when run as the scheduled task. Always reports the
        pending count BEFORE the drain attempt so a stalled drain is visible
        next morning via the tripwire line.

        VM leg: reports pending count + index stats (read-only view) but cannot
        drain (no signing key).

        The recent-notes list is routed through the SAME egress.apply_gate
        chokepoint as every other read verb (H-1) — a summary surface must not
        leak titles/paths/classification of withheld-tier notes.
        """
        from .. import brief as brief_mod
        from .. import egress
        from ..snapshot import snapshot_status

        pending_before = self._count_pending_drafts()
        drain_res: dict[str, Any] = {"promoted": 0, "skipped": 0}

        if self.role == config.ROLE_HOST and drain:
            try:
                drain_res = self.drain_drafts()
            except Exception as exc:
                drain_res = {"promoted": 0, "skipped": 0, "error": str(exc)}

        try:
            stats = self.index.stats()
        except Exception:
            stats = {"notes": 0, "chunks": 0}

        try:
            recent = self.recent(limit=max_recent)
        except Exception:
            recent = []

        surfaced, egress_report = egress.apply_gate(recent, max_tier=max_tier)

        snap = snapshot_status(config.snapshot_dir(self.vault))
        age_hours: float | None = None
        if snap.get("snapshot") == "present" and snap.get("age_seconds") is not None:
            age_hours = snap["age_seconds"] / 3600

        cos_liveness: dict[str, Any] | None = None
        if self.role == config.ROLE_HOST:
            try:
                from .. import cos as cos_mod
                cos_liveness = cos_mod.batch_liveness(self.vault)
            except Exception:  # noqa: BLE001 — a liveness read never breaks the brief
                cos_liveness = None

        result = brief_mod.build_brief(
            index_stats=stats,
            recent_notes=surfaced,
            pending_before_drain=pending_before,
            drain_result=drain_res,
            snapshot_age_hours=age_hours,
            max_recent=max_recent,
            maintain_state=self._load_maintain_state(),
            cos_liveness=cos_liveness,
        )
        result["egress"] = egress_report
        return result
    def digest(
        self, *, days: int = 7, max_tier: str = classification.DEFAULT_MAX_TIER,
    ) -> dict[str, Any]:
        """Generate the weekly digest (UX-02).

        Shows notes from the past ``days`` days. Available on both host and VM
        legs (read-only; reads from the index/snapshot in use for this role).

        The recent-notes list is gated through egress.apply_gate before it is
        built into the digest (H-1) — same chokepoint as every other read verb.
        """
        from .. import brief as brief_mod
        from .. import egress

        try:
            stats = self.index.stats()
        except Exception:
            stats = {"notes": 0, "chunks": 0}

        try:
            recent = self.recent(limit=500)
        except Exception:
            recent = []

        surfaced, egress_report = egress.apply_gate(recent, max_tier=max_tier)

        result = brief_mod.build_digest(
            index_stats=stats, recent_notes=surfaced, days=days,
            maintain_state=self._load_maintain_state(),
        )
        result["egress"] = egress_report
        return result
    def _autoresearch_status(self, today: Any) -> dict[str, Any]:
        """Maintenance-visibility line data (HARDENED:claude, AUT-01): scan
        ``eval/runs/autoresearch-*.json`` for the newest ``captured``
        timestamp and judge staleness via the pure
        ``maintenance.autoresearch_staleness`` helper. No autoresearch run has
        landed at this session (aut-04 is session s11, after this one) — a
        missing/unreadable artifact is treated as ``never_run``, never an
        error, so the brief still renders."""
        import datetime as _dt
        import json

        from .. import maintenance as maint

        latest: _dt.datetime | None = None
        repo_root = source_repo_root()
        if repo_root is not None:
            runs_dir = repo_root / "eval" / "runs"
            try:
                for p in runs_dir.glob("autoresearch-*.json"):
                    try:
                        data = json.loads(p.read_text(encoding="utf-8"))
                        ts = _dt.datetime.fromisoformat(str(data.get("captured")))
                    except Exception:
                        continue
                    if latest is None or ts > latest:
                        latest = ts
            except Exception:
                pass
        return maint.autoresearch_staleness(latest.date() if latest else None, today)
    def brief_html(
        self, *, max_recent: int = 5, drain: bool = True,
        max_tier: str = classification.VM_DEFAULT_MAX_TIER, today: Any = None,
    ) -> dict[str, Any]:
        """Render + write the branded HTML morning brief (AUT-01, ADR-0003
        Ruling c) to ``.brain/brief/``. HOST-ONLY: this writes a FILE, a new
        egress surface the stdout gate does not cover, so every section is
        composed from data already routed through ``egress.apply_gate`` at
        ``max_tier`` (default Internal) before it reaches the pure renderer
        (``brain.brief.render_brief_html``), which does no I/O of its own —
        gathering + gating happens entirely here.
        """
        import datetime as _dt

        from .. import brief as brief_mod
        from .. import egress
        from .. import maintenance as maint
        from .. import overlay as ov

        self._require_host("write the HTML morning brief")
        d = today or _dt.date.today()
        flt = classification.ClassificationFilter(max_tier=max_tier)

        base = self.brief(max_recent=max_recent, drain=drain, max_tier=max_tier)

        try:
            stale_links = self.index.stale_wikilink_targets()
        except Exception:
            stale_links = []
        stale_links = [
            s for s in stale_links
            if flt.allows((s.get("from") or {}).get("classification"))
            and (s.get("target") is None or flt.allows((s.get("target") or {}).get("classification")))
        ]

        try:
            revisit_sample = self.index.revisit_sample(today=d, k=10)
        except Exception:
            revisit_sample = []
        revisit_sample, _ = egress.apply_gate(revisit_sample, max_tier=max_tier)

        open_recs: list[dict[str, Any]] = []
        try:
            open_path = config.recommendations_open_path(self.vault)
            if open_path.exists():
                open_recs = maint.parse_recommendation_lines(open_path.read_text(encoding="utf-8"))
        except Exception:
            open_recs = []

        hot_head: list[str] = []
        try:
            hot_path = self._hot_md_path()
            if hot_path.exists():
                hot_head = brief_mod.parse_hot_entries(hot_path.read_text(encoding="utf-8"))[-5:]
        except Exception:
            hot_head = []

        autoresearch = self._autoresearch_status(d)
        brand = ov.resolve_brand(self.vault)

        html_text = brief_mod.render_brief_html(
            base, stale_links=stale_links, revisit_sample=revisit_sample,
            open_recommendations=open_recs, hot_head=hot_head,
            autoresearch=autoresearch, brand=brand,
        )

        out_dir = config.brief_dir(self.vault)
        out_dir.mkdir(parents=True, exist_ok=True)
        dated = out_dir / f"brief-{d.isoformat()}.html"
        latest = out_dir / "brief-latest.html"
        dated.write_text(html_text, encoding="utf-8")
        latest.write_text(html_text, encoding="utf-8")
        return {"path": str(dated), "latest_path": str(latest), "bytes": len(html_text)}
    def digest_html(
        self, *, days: int = 7, max_tier: str = classification.VM_DEFAULT_MAX_TIER,
        today: Any = None,
    ) -> dict[str, Any]:
        """Render + write the branded HTML weekly digest (AUT-03, ADR-0003
        Ruling c) to ``.brain/brief/``. HOST-ONLY (writes a file). Notes are
        already routed through ``egress.apply_gate`` inside ``self.digest()``
        before the pure renderer (``brain.brief.render_digest_html``) formats
        them — the renderer performs no I/O."""
        import datetime as _dt

        from .. import brief as brief_mod
        from .. import overlay as ov

        self._require_host("write the HTML weekly digest")
        d = today or _dt.date.today()

        base = self.digest(days=days, max_tier=max_tier)
        brand = ov.resolve_brand(self.vault)
        html_text = brief_mod.render_digest_html(base, brand=brand)

        out_dir = config.brief_dir(self.vault)
        out_dir.mkdir(parents=True, exist_ok=True)
        dated = out_dir / f"digest-{d.isoformat()}.html"
        latest = out_dir / "digest-latest.html"
        dated.write_text(html_text, encoding="utf-8")
        latest.write_text(html_text, encoding="utf-8")
        return {"path": str(dated), "latest_path": str(latest), "bytes": len(html_text)}
    def health_report(self, *, today: Any = None) -> dict[str, Any]:
        """Render + write the static HTML health report (``brain
        health-report``) to ``.brain/brief/health-latest.html`` — a single
        colored verdict (HEALTHY/DEGRADED/BROKEN) + an "act now" list +
        maintain-branch/index/snapshot/trend tables, assembled entirely from
        data the engine already collects (maintain-state.json,
        health-history.jsonl, ``brain doctor``, ``status()``). HOST-ONLY:
        writes a file, same posture as ``brief_html``/``digest_html``. See
        ``brain.healthreport`` for the render contract."""
        from .. import healthreport as hr

        self._require_host("render the health report")
        data = hr.collect_health_report_data(self, today=today)
        html_text = hr.render_health_report_html(data)

        out_dir = config.brief_dir(self.vault)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "health-latest.html"
        path.write_text(html_text, encoding="utf-8")
        return {"path": str(path), "verdict": data["verdict"], "act_now": data["act_now"]}
    def graph_report(self, *, today: Any = None) -> dict[str, Any]:
        """Render + write the static HTML graph explorer (``brain
        graph-report``) to ``.brain/graph/graph-explorer.html`` — a
        self-contained WebGL link-graph + 3D semantic-map page, rebuilt from
        the graphify discovery build (``.brain/graph/graph.json``,
        ``authoritative: false``) plus the live index. HOST-ONLY: reads the
        writable index, writes a file. See ``brain.graphreport`` for the
        payload/render contract. Never crashes on missing embeddings — see
        that module's ``semantic_note`` degrade path."""
        from .. import graphreport as gr

        self._require_host("render the graph report")
        return gr.generate_graph_report(self, today=today)
