"""Smoke half of ``export_check_no_cos.py``: the core verbs on a made-up vault.

Runs ``init``, one ``.md`` and one ``.eml`` dropped in ``inbox/``, ``sync``,
``search``, ``get``, ``recent``, ``draft-capture``, ``doctor`` and
``maintain --json`` against the ``brain`` package found under ``src_dir``,
inside a throwaway HOME with the hash embedder. Nothing here reads the
caller's vault, keychain, registry or network.

``maintain --json`` is judged against FROZEN expectations kept in this file,
so the shipped checker needs no private evidence. They were derived on
2026-09-22 from the same synthetic vault run in the private checkout with COS
present (plan evidence ``s01-maintain-reference.json``). Folds record a failure
only as ``str(exc)`` in the ``blocked`` bucket and often leave no result key, so
the judge requires both: zero blocked items, and every required key present
and passing its predicate.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

TIMEOUT = 240

MD_DROP = "# Zebra migration notes\n\nThe zebra herd crossed the river in spring.\n"
EML_DROP = (
    "From: Alice Example <alice@example.org>\n"
    "To: Bob Example <bob@example.org>\n"
    "Subject: Quarterly aardvark budget\n"
    "Date: Mon, 21 Sep 2026 10:00:00 +0000\n"
    "Message-ID: <smoke-1@example.org>\n"
    "Content-Type: text/plain; charset=utf-8\n"
    "\n"
    "The aardvark budget for next quarter is approved.\n"
)


def _has(*keys: str) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, dict) and all(k in v for k in keys)


def _clean(*keys: str, errors: str = "errors") -> Callable[[Any], bool]:
    """Has ``keys`` and its ``errors`` field (when present) is empty."""
    return lambda v: _has(*keys)(v) and not v.get(errors)


# Required non-COS result keys of `maintain --json` on a fresh synthetic vault,
# each with one success predicate. Derived from the COS-present reference run.
MAINTAIN_REQUIRED: dict[str, Callable[[Any], bool]] = {
    "sync": lambda v: _has("indexed", "ingest")(v) and v["indexed"] >= 1,
    "remediation": _has("branches"),
    "version_chains": _clean("chained"),
    "autodedup": _has("retired"),
    "auto_para": _clean("moved"),
    "egress_ring": _has("terms", "path"),
    "navigation": _has("backlink_targets", "catalog_counts"),
    "kl_orphans": _has("orphan_count"),
    "retention": _has("pruned"),
    "query_capture_retention": _clean("pruned"),
    "deliverables_previous_retention": _has("pruned"),
    "quarantine_summary": lambda v: _has("total")(v) and v["total"] == 0,
    "decision_capture": _has("candidates"),
    "deliverables_shelf": _has("census"),
    "corpus_invariants": _has("unsigned_notes", "unlinked_sources"),
    "link_lane": _has("total_unlinked"),
    "sync_publish": _has("added"),
    "brief": _has("notes", "date"),
    "brief_html": _has("path", "bytes"),
    "recommendations_aging": _has("scanned"),
    "daily_note": _has("created"),
    "graphify_drift": _has("triggered"),
    "health": _has("status", "audit"),
    "integrity": _has("audit"),
    "graph_hygiene": _has("orphan_count"),
    "digest": _has("notes_total"),
    "curate": _has("stale_links"),
    "promote_scan": _has("candidates"),
    "digest_html": _has("path", "bytes"),
    "retro": _has("findings"),
    "golden": _has("score"),
    "graphify": _has("invoked"),
    "health_history": _has("blocked"),
    "health_trend": lambda v: isinstance(v, list),
    "notifications": lambda v: isinstance(v, list),
    "auto_update": _has("auto_update"),
    "exceptions_page": _has("count"),
}

# Blocked findings that also appear with COS present are pre-existing defects,
# listed here one by one with a justification. None were measured.
MAINTAIN_KNOWN_BLOCKED: dict[str, str] = {}


def judge_maintain(doc: Any) -> list[str]:
    """Findings for one `maintain --json` document; empty means it passed."""
    if not isinstance(doc, dict):
        return ["maintain --json: output is not a JSON object"]
    findings: list[str] = []
    blocked = (doc.get("outcomes") or {}).get("blocked")
    if not isinstance(blocked, list):
        findings.append("maintain --json: outcomes.blocked missing")
        blocked = []
    for item in blocked:
        text = str(item.get("finding") if isinstance(item, dict) else item)
        if not any(text.startswith(k) for k in MAINTAIN_KNOWN_BLOCKED):
            findings.append(f"maintain blocked: {text}")
    results = doc.get("results") if isinstance(doc.get("results"), dict) else {}
    for key, pred in MAINTAIN_REQUIRED.items():
        if key not in results:
            findings.append(f"maintain result missing: {key}")
        elif not pred(results[key]):
            findings.append(f"maintain result {key} failed its predicate: {str(results[key])[:200]}")
    return findings


def _ephemeral_key() -> str:
    from cryptography.hazmat.primitives import serialization as s
    from cryptography.hazmat.primitives.asymmetric import ed25519
    return ed25519.Ed25519PrivateKey.generate().private_bytes(
        s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption()).decode("ascii")


def isolated_env(src_dir: Path, work: Path) -> dict[str, str]:
    """A child environment that cannot reach the caller's home, vault or keychain."""
    home, stub = work / "home", work / "bin"
    for d in (home, stub, work / "app"):
        d.mkdir(parents=True, exist_ok=True)
    security = stub / "security"          # macOS keychain CLI: always refuse
    security.write_text("#!/bin/sh\nexit 1\n")
    security.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BRAIN", "PYTHON"))}
    env.update({
        "HOME": str(home), "PATH": f"{stub}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHONPATH": str(src_dir), "PYTHONDONTWRITEBYTECODE": "1",
        "BRAIN_APP_DATA_DIR": str(work / "app"), "BRAINIAC_HOME": str(home / ".brainiac"),
        "BRAIN_VAULT": str(work / "vault"), "BRAIN_EMBEDDER": "hash",
        "BRAIN_NOTIFY": "off", "BRAIN_RERANKER_PREFER": "noop",
        "BRAIN_AUDIT_KEY_PEM": _ephemeral_key(),
    })
    return env


class Smoke:
    def __init__(self, src_dir: Path, work: Path, python: str = sys.executable):
        self.src_dir, self.work, self.python = src_dir, work, python
        self.vault = work / "vault"
        self.env = isolated_env(src_dir, work)
        self.findings: list[str] = []
        self.log: list[str] = []
        self.maintain_doc: Any = None

    def run(self, *args: str, ok_codes: tuple[int, ...] = (0,)) -> Any:
        """Run one brain verb; return parsed JSON stdout, or None on failure."""
        argv = [self.python, "-B", "-m", "brain", "--vault", str(self.vault), *args]
        try:
            p = subprocess.run(argv, capture_output=True, text=True,
                               env=self.env, cwd=self.work, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            self.findings.append(f"smoke `{' '.join(args)}` timed out after {TIMEOUT}s")
            return None
        self.log.append(f"brain {' '.join(args)} -> exit {p.returncode}")
        if p.returncode not in ok_codes:
            tail = (p.stderr or p.stdout).strip().splitlines()[-3:]
            self.findings.append(f"smoke `{' '.join(args)}` exit {p.returncode}: {' | '.join(tail)}")
            return None
        try:
            return json.loads(p.stdout)
        except json.JSONDecodeError:
            self.findings.append(f"smoke `{' '.join(args)}`: stdout is not JSON")
            return None

    def check_origin(self) -> bool:
        code = "import brain; print(brain.__file__)"
        p = subprocess.run([self.python, "-B", "-c", code], capture_output=True, text=True,
                           env=self.env, cwd=self.work, timeout=TIMEOUT)
        origin = p.stdout.strip()
        self.log.append(f"brain.__file__ = {origin or p.stderr.strip()[-300:]}")
        if not origin.startswith(str(self.src_dir.resolve())) and not origin.startswith(str(self.src_dir)):
            self.findings.append(f"smoke: `brain` imports from {origin or '(nowhere)'}, not {self.src_dir}")
            return False
        return True

    def ingest(self) -> None:
        inbox = self.vault / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "zebra-notes.md").write_text(MD_DROP, encoding="utf-8")
        (inbox / "budget.eml").write_text(EML_DROP, encoding="utf-8")
        doc = self.run("sync", "--json")
        ingest = (doc or {}).get("ingest") or {}
        done = {row.get("file") for row in ingest.get("processed") or []}
        for name in ("zebra-notes.md", "budget.eml"):
            if doc is not None and name not in done:
                self.findings.append(f"smoke sync: {name} was not ingested")
        if ingest.get("quarantined"):
            self.findings.append(f"smoke sync: quarantined {ingest['quarantined']}")

    def read_verbs(self) -> None:
        for word, stem in (("zebra", "zebra-notes"), ("aardvark", "budget")):
            doc = self.run("search", word, "--json")
            hits = [str(r.get("id")) for r in (doc or {}).get("results") or []]
            if doc is not None and not any(h.endswith(stem) for h in hits):
                self.findings.append(f"smoke search {word!r}: the {stem} drop is not in the results {hits}")
        ids = [r.get("id") for r in ((self.run("recent", "--json") or {}).get("results") or [])]
        if not ids:
            self.findings.append("smoke recent: no notes listed")
        else:
            got = self.run("get", ids[0], "--json")
            if got is not None and got.get("id") != ids[0]:
                self.findings.append(f"smoke get {ids[0]}: returned {got.get('id')!r}")
        draft = self.run("draft-capture", "--content", "Smoke draft: a staged note.", "--json")
        if draft is not None and not Path(str(draft.get("draft", ""))).is_file():
            self.findings.append("smoke draft-capture: no draft file staged")
        # doctor exits 1 on ANY stale row, and the smoke's interpreter carries
        # the installed package's version, which lags the tag right after a
        # bump. The smoke proves doctor RUNS without COS, so it judges the JSON.
        doctor = self.run("doctor", "--json", ok_codes=(0, 1))
        if doctor is not None and not doctor.get("rows"):
            self.findings.append("smoke doctor: no rows reported")

    def all(self) -> list[str]:
        if not self.check_origin():
            return self.findings
        init = self.run("init", "--full", "--no-register-tasks", "--yes", "--json")
        if init is None or not init.get("ok"):
            self.findings.append("smoke init: did not report ok")
            return self.findings
        self.ingest()
        self.read_verbs()
        self.maintain_doc = self.run("maintain", "--json")
        if self.maintain_doc is not None:
            self.findings += judge_maintain(self.maintain_doc)
        return self.findings
