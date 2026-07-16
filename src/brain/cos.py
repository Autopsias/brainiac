"""COS host-engine capabilities (CUT-01E) — proposal broker, evidence signer,
correction transport, priority-map generator, auto-capture hold store.

Every verb here that mutates state of record is HOST-BROKER ONLY. The single
VM-allowed ingress is ``cos-propose``: an UNSIGNED drop into a proposal-drop
directory that ``brain sync`` NEVER reads — a proposal can only reach the
signed write path through the broker's validate → owner-inbox-batch →
on-accept → capture-inbox flow.

Directory layout (``config.cos_ops_dir``, default ``<vault>/.brain/cos`` —
gitignored wholesale via ``vault/.brain/``, never indexed, never exported),
split by PERMISSION:

    cos/
    ├── host/                # (a) HOST-PRIVATE (0700) — the VM never reads or
    │   │                    #     writes below here.
    │   ├── evidence/        #     signed trust-gate evidence bundles
    │   ├── proposals/       #     broker queue OF RECORD
    │   │   ├── pending/     #       claimed, validated, awaiting a batch/answer
    │   │   ├── rejected/    #       failed validation / owner-rejected
    │   │   ├── expired/     #       TTL-expired (GC-compacted later)
    │   │   ├── corrections-pending/  # VM correction drops awaiting the owner
    │   │   ├── claims.jsonl #       content-hash claim ledger (anti-replay)
    │   │   └── batches.jsonl#       cos_ingestion_batch records (state machine)
    │   ├── hold/            #     auto-capture hold store (not_before gated)
    │   └── corrections.sqlite  #  correction_events of record (append-only)
    ├── shared/              # (b) VM-READABLE projection (0755) — host writes,
    │   └── priority-map.md  #     VM reads. Never host-private content.
    └── drop/                # (c) VM-WRITABLE input (0775) — VM appends, host
        ├── proposal-drop/   #     claims. `brain sync` IGNORES all of drop/.
        └── verdict-drop/    #     shadow-ledger + correction drops (s04)

Proposal lifecycle: dropped → pending (claimed+validated) → queued (member of
an open, signed ``cos_ingestion_batch`` owner-inbox question) → accepted
(moved to capture-inbox/, signed by the ordinary host drain) | rejected |
expired (TTL) → GC. Backpressure: at most ONE open batch at a time (the owner
queue is ~5-capped); new proposals aggregate into the next batch. A batch
answer is verified against the batch's Ed25519-signed candidate-set digest,
must be a SUBSET of the batch, and is consumed exactly once — a replayed or
late (post-expiry) answer is rejected.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from . import config, frontmatter
from .notes import safe_slug, sha256_text

# -- tunables ---------------------------------------------------------------
PROPOSAL_TTL_DAYS_ENV = "BRAIN_COS_PROPOSAL_TTL_DAYS"
DEFAULT_PROPOSAL_TTL_DAYS = 14
BATCH_TTL_DAYS_ENV = "BRAIN_COS_BATCH_TTL_DAYS"
DEFAULT_BATCH_TTL_DAYS = 7
GC_DAYS_ENV = "BRAIN_COS_GC_DAYS"
DEFAULT_GC_DAYS = 30

BATCH_SCHEMA = "cos_ingestion_batch/v1"
EVIDENCE_SCHEMA = "cos-evidence-manifest/v1"
BROKER_KEY_PREFIX = "cosbroker:"
CORRECT_KEY_PREFIX = "coscorrect:"

# Answer grammar for a cos_ingestion_batch owner question.
_ACCEPT_ALL = "accept all"
_REJECT_ALL = "reject all"
_ACCEPT_PARTIAL_RE = re.compile(r"^accept\s*:\s*(?P<ids>[a-z0-9,\s-]+?)\s*(?:\(.*\))?$",
                                re.IGNORECASE)

# Secret-scrub patterns (trust boundary: VM drops are attacker-influenced;
# a proposal must never smuggle credentials into the signed vault).
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer-ish-key", re.compile(r"\b(?:sk|ghp|gho|glpat)[-_][A-Za-z0-9_-]{20,}\b")),
    ("kv-secret", re.compile(
        r"(?im)^\s*(?:api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*\S{8,}")),
)


def secret_findings(text: str) -> list[str]:
    """Names of secret patterns present in ``text`` (empty = clean)."""
    return [name for name, pat in _SECRET_PATTERNS if pat.search(text)]


# -- layout -----------------------------------------------------------------
def ops_dir(vault: Path | str | None = None) -> Path:
    return config.cos_ops_dir(vault)


def host_dir(vault=None) -> Path:
    return ops_dir(vault) / "host"


def shared_dir(vault=None) -> Path:
    return ops_dir(vault) / "shared"


def drop_dir(vault=None) -> Path:
    return ops_dir(vault) / "drop"


def proposal_drop_dir(vault=None) -> Path:
    return drop_dir(vault) / "proposal-drop"


def verdict_drop_dir(vault=None) -> Path:
    return drop_dir(vault) / "verdict-drop"


def evidence_dir(vault=None) -> Path:
    return host_dir(vault) / "evidence"


def proposals_dir(vault=None) -> Path:
    return host_dir(vault) / "proposals"


def hold_dir(vault=None) -> Path:
    return host_dir(vault) / "hold"


def corrections_db_path(vault=None) -> Path:
    return host_dir(vault) / "corrections.sqlite"


def priority_map_path(vault=None) -> Path:
    return shared_dir(vault) / "priority-map.md"


# Documented permission per sub-path (best-effort chmod; VirtioFS/Windows may
# only partially honour POSIX bits — the split is ALSO enforced behaviourally:
# no VM verb ever resolves a path under host/).
_PERMS = {"host": 0o700, "shared": 0o755, "drop": 0o775}


def ensure_layout(vault=None) -> dict[str, str]:
    """Create the three permission zones + their sub-dirs (idempotent)."""
    zones = {
        "host": host_dir(vault),
        "shared": shared_dir(vault),
        "drop": drop_dir(vault),
    }
    for name, d in zones.items():
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, _PERMS[name])
        except OSError:
            pass
    for d in (evidence_dir(vault), proposals_dir(vault), hold_dir(vault),
              proposals_dir(vault) / "pending", proposals_dir(vault) / "rejected",
              proposals_dir(vault) / "expired",
              proposals_dir(vault) / "corrections-pending"):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)  # nosemgrep: insecure-file-permissions -- intentionally OWNER-ONLY (host-private zone), not overly-permissive
        except OSError:
            pass
    for d in (proposal_drop_dir(vault), verdict_drop_dir(vault),
              ingest_manifest_dir(vault)):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o775)  # nosemgrep: insecure-file-permissions -- VM-writable drop zone needs group-write; owner+group only, no world access
        except OSError:
            pass
    return {str(p): oct(_PERMS[n]) for n, p in zones.items()}


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _ts(dt: _dt.datetime | None = None) -> str:
    return (dt or _utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(s: str) -> _dt.datetime | None:
    try:
        out = _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return out if out.tzinfo else out.replace(tzinfo=_dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def _env_days(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env, default))
    except ValueError:
        return default


# -- VM ingress: cos-propose --------------------------------------------------
def propose(vault, content: str, *, ident: str | None = None) -> dict[str, Any]:
    """Write ONE unsigned proposal candidate into ``drop/proposal-drop/``.

    VM-ALLOWED. Never signs, never indexes, never touches capture-inbox — the
    ordinary ``brain sync`` drain does not read this directory, so nothing
    dropped here can reach the signed write path without the broker.
    """
    from . import capture as cap_mod

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
    ddir = proposal_drop_dir(vault)
    ddir.mkdir(parents=True, exist_ok=True)
    target = ddir / f"{note_id}.md"
    if target.resolve().parent != ddir.resolve():
        raise ValueError(f"proposal target escapes drop dir: {note_id!r}")
    target.write_text(staged, encoding="utf-8")
    return {"proposal": str(target), "id": note_id, "signed": False,
            "state": "dropped",
            "note": "unsigned proposal drop; the host broker validates, asks the "
                    "owner, and only an ACCEPTED candidate is ever signed"}


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
    target.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
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


# -- claims ledger ------------------------------------------------------------
def _claims_path(vault) -> Path:
    return proposals_dir(vault) / "claims.jsonl"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


# -- broker: claim drops -------------------------------------------------------
def claim_drops(vault, now: _dt.datetime | None = None) -> dict[str, Any]:
    """Validate + claim every proposal drop into ``host/proposals/pending/``.

    HOST side of the trust boundary. Each drop is: schema-validated
    (``capture.validate``), classification-checked, secret-scrubbed, and
    replay-checked against the content-hash claims ledger. A drop that fails
    any check is moved to ``rejected/`` (never signed, never silently lost);
    a replayed drop (hash already claimed) is deleted and logged."""
    from . import capture as cap_mod

    now = now or _utcnow()
    claimed: list[str] = []
    rejected: list[dict[str, str]] = []
    replayed: list[str] = []
    ledger = _read_jsonl(_claims_path(vault))
    seen_hashes = {e.get("sha256") for e in ledger}
    pending = proposals_dir(vault) / "pending"
    rej_dir = proposals_dir(vault) / "rejected"
    ddir = proposal_drop_dir(vault)
    if not ddir.is_dir():
        return {"claimed": [], "rejected": [], "replayed": []}
    pending.mkdir(parents=True, exist_ok=True)
    rej_dir.mkdir(parents=True, exist_ok=True)

    ttl_days = _env_days(PROPOSAL_TTL_DAYS_ENV, DEFAULT_PROPOSAL_TTL_DAYS)
    for f in sorted(ddir.glob("*.md")):
        # Trust boundary: the drop dir is VM-writable. A symlink here could
        # smuggle host-side content past validation (read-through) or be
        # target-swapped after validation (TOCTOU) — only regular files are
        # ever claimed; a symlink is deleted and logged, never followed.
        if f.is_symlink() or not f.is_file():
            f.unlink(missing_ok=True)
            rejected.append({"drop": f.name, "reason": "not a regular file (symlink refused)"})
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError as exc:
            rejected.append({"drop": f.name, "reason": f"unreadable: {exc}"})
            continue
        sha = sha256_text(text)
        if sha in seen_hashes:
            f.unlink(missing_ok=True)
            replayed.append(f.name)
            _append_jsonl(_claims_path(vault),
                          {"sha256": sha, "drop": f.name, "ts": _ts(now),
                           "disposition": "replay-rejected"})
            continue
        reasons: list[str] = list(cap_mod.validate(text))
        secrets = secret_findings(text)
        if secrets:
            reasons.append("secret-scrub: " + ", ".join(secrets))
        meta, _ = frontmatter.parse_text(text)
        if secrets:
            # ING-04 defect signal: a claim-time secret/classification finding
            # disqualifies the candidate's PATTERN from auto-capture eligibility
            # outright (zero-tolerance), regardless of this candidate's own fate.
            record_outcome(vault, pattern=meta.get("pattern"), ident=f.stem,
                           outcome="claim-rejected-security",
                           bundle_version=meta.get("bundle_version"), ts=_ts(now))
        nid = None
        try:
            nid = safe_slug(meta.get("id") or f.stem)
        except ValueError as exc:
            reasons.append(f"unsafe id: {exc}")
        if nid and not reasons and (pending / f"{nid}.md").exists():
            reasons.append(f"duplicate pending id: {nid!r}")
        if reasons:
            dest = rej_dir / f"{now.strftime('%Y%m%dT%H%M%S')}-{f.name}"
            shutil.move(str(f), dest)
            rejected.append({"drop": f.name, "reason": "; ".join(reasons)})
            _append_jsonl(_claims_path(vault),
                          {"sha256": sha, "drop": f.name, "ts": _ts(now),
                           "disposition": "rejected: " + "; ".join(reasons)})
            continue
        dest = pending / f"{nid}.md"
        shutil.move(str(f), dest)
        meta_path = pending / f"{nid}.json"
        meta_path.write_text(json.dumps({
            "id": nid, "sha256": sha, "claimed": _ts(now),
            "ttl_expires": _ts(now + _dt.timedelta(days=ttl_days)),
            "state": "pending",
        }, sort_keys=True) + "\n", encoding="utf-8")
        claimed.append(nid)
        seen_hashes.add(sha)
        _append_jsonl(_claims_path(vault),
                      {"sha256": sha, "id": nid, "ts": _ts(now),
                       "disposition": "claimed"})

    # Correction drops (verdict-drop/correction-*.json) → corrections-pending/.
    corrections_claimed: list[str] = []
    vdir = verdict_drop_dir(vault)
    cor_pending = proposals_dir(vault) / "corrections-pending"
    if vdir.is_dir():
        cor_pending.mkdir(parents=True, exist_ok=True)
        for f in sorted(vdir.glob("correction-*.json")):
            if f.is_symlink() or not f.is_file():  # same symlink refusal as above
                f.unlink(missing_ok=True)
                rejected.append({"drop": f.name,
                                 "reason": "not a regular file (symlink refused)"})
                continue
            try:
                payload = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = None
            errs = _validate_correction_payload(payload)
            if errs:
                dest = rej_dir / f"{now.strftime('%Y%m%dT%H%M%S')}-{f.name}"
                shutil.move(str(f), dest)
                rejected.append({"drop": f.name, "reason": "; ".join(errs)})
                continue
            shutil.move(str(f), cor_pending / f.name)
            corrections_claimed.append(f.name)

    return {"claimed": claimed, "rejected": rejected, "replayed": replayed,
            "corrections_claimed": corrections_claimed}


# -- proposal state helpers ----------------------------------------------------
def _pending_metas(vault) -> list[dict[str, Any]]:
    pending = proposals_dir(vault) / "pending"
    out = []
    if not pending.is_dir():
        return out
    for meta_path in sorted(pending.glob("*.json")):
        try:
            m = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(m, dict) and (pending / f"{m.get('id')}.md").exists():
            out.append(m)
    return out


def expire_proposals(vault, now: _dt.datetime | None = None) -> list[str]:
    """Move TTL-expired pending proposals to ``expired/`` (never signed)."""
    now = now or _utcnow()
    pending = proposals_dir(vault) / "pending"
    expired_dir = proposals_dir(vault) / "expired"
    expired: list[str] = []
    for m in _pending_metas(vault):
        exp = _parse_ts(m.get("ttl_expires", ""))
        if exp and exp <= now:
            expired_dir.mkdir(parents=True, exist_ok=True)
            for suffix in (".md", ".json"):
                src = pending / f"{m['id']}{suffix}"
                if src.exists():
                    shutil.move(str(src), expired_dir / src.name)
            expired.append(m["id"])
    return expired


def gc_compact(vault, now: _dt.datetime | None = None) -> dict[str, int]:
    """Delete rejected/expired artifacts older than the GC window."""
    now = now or _utcnow()
    cutoff = now.timestamp() - _env_days(GC_DAYS_ENV, DEFAULT_GC_DAYS) * 86400
    removed = 0
    for sub in ("rejected", "expired"):
        d = proposals_dir(vault) / sub
        if not d.is_dir():
            continue
        for f in d.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                continue
    # Compact consumed/expired batch records older than the GC window.
    bpath = _batches_path(vault)
    batches = _read_jsonl(bpath)
    keep = []
    dropped = 0
    for b in batches:
        closed = b.get("state") in ("consumed", "expired", "invalid")
        ts = _parse_ts(b.get("consumed_at") or b.get("expired_at") or b.get("created", ""))
        if closed and ts and ts.timestamp() < cutoff:
            dropped += 1
            continue
        keep.append(b)
    if dropped:
        bpath.write_text(
            "".join(json.dumps(b, sort_keys=True) + "\n" for b in keep),
            encoding="utf-8")
    return {"files_removed": removed, "batches_compacted": dropped}


# -- batches --------------------------------------------------------------------
def _batches_path(vault) -> Path:
    return proposals_dir(vault) / "batches.jsonl"


def _write_batches(vault, batches: list[dict[str, Any]]) -> None:
    p = _batches_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(b, sort_keys=True) + "\n" for b in batches),
                 encoding="utf-8")


def batch_digest(batch_id: str, created: str, candidates: list[dict[str, str]]) -> str:
    """Canonical digest over the candidate SET (order-independent)."""
    canon = json.dumps(
        {"batch_id": batch_id, "created": created, "schema": BATCH_SCHEMA,
         "candidates": sorted(candidates, key=lambda c: c["id"])},
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def open_batches(vault) -> list[dict[str, Any]]:
    return [b for b in _read_jsonl(_batches_path(vault)) if b.get("state") == "open"]


def enqueue_batch(core, now: _dt.datetime | None = None) -> dict[str, Any]:
    """Aggregate ALL pending proposals into ONE signed cos_ingestion_batch and
    enqueue exactly one owner-inbox question for it.

    Backpressure (ing-02): refuses while another batch is open — the owner
    queue holds at most one broker slot; new proposals wait in ``pending`` and
    join the next batch. Fails CLOSED if no signing key resolves (nothing is
    enqueued unsigned)."""
    from . import audit

    now = now or _utcnow()
    vault = core.vault
    if open_batches(vault):
        return {"enqueued": False, "reason": "batch-already-open (backpressure)"}
    metas = _pending_metas(vault)
    # Exclude proposals already queued in a (still-open) batch — defensive; an
    # open batch already blocks above.
    if not metas:
        return {"enqueued": False, "reason": "no-pending-proposals"}
    candidates = [{"id": m["id"], "sha256": m["sha256"]} for m in metas]
    batch_id = "cosb-" + hashlib.sha256(
        (_ts(now) + json.dumps(candidates, sort_keys=True)).encode()).hexdigest()[:12]
    created = _ts(now)
    digest = batch_digest(batch_id, created, candidates)
    key_obj, _src = audit.resolve_signing_key()  # KeyUnavailable → fail closed
    sig = key_obj.sign(digest.encode("utf-8")).hex()
    ttl_days = _env_days(BATCH_TTL_DAYS_ENV, DEFAULT_BATCH_TTL_DAYS)
    record = {
        "schema": BATCH_SCHEMA, "batch_id": batch_id, "created": created,
        "candidates": candidates, "digest": digest, "sig": sig,
        "state": "open", "expires": _ts(now + _dt.timedelta(days=ttl_days)),
    }
    batches = _read_jsonl(_batches_path(vault))
    batches.append(record)
    _write_batches(vault, batches)

    ids = [c["id"] for c in candidates]
    question = {
        "key": BROKER_KEY_PREFIX + batch_id,
        "question": (f"COS ingestion batch {batch_id}: {len(ids)} candidate "
                     f"note(s) await approval before signing: {', '.join(ids)}"),
        "options": [_ACCEPT_ALL, _REJECT_ALL,
                    "accept: <id,id,...> (partial — list the ids to accept)"],
        "default": _REJECT_ALL,
        "context": f"schema={BATCH_SCHEMA} digest={digest[:16]}… "
                   f"expires={record['expires']}. Only accepted candidates are "
                   f"ever signed; unanswered batches expire and requeue.",
    }
    core.enqueue_question(question, source=f"cos-broker:{batch_id}",
                          today=now.date())
    return {"enqueued": True, "batch_id": batch_id, "candidates": ids,
            "digest": digest}


def expire_batches(vault, now: _dt.datetime | None = None) -> list[str]:
    """Expire open batches past their TTL. Their candidates REQUEUE (stay in
    ``pending/``) and join the next batch; a late answer to an expired batch
    is rejected by ``consume_answers``."""
    now = now or _utcnow()
    batches = _read_jsonl(_batches_path(vault))
    expired: list[str] = []
    changed = False
    for b in batches:
        if b.get("state") != "open":
            continue
        exp = _parse_ts(b.get("expires", ""))
        if exp and exp <= now:
            b["state"] = "expired"
            b["expired_at"] = _ts(now)
            expired.append(b["batch_id"])
            changed = True
    if changed:
        _write_batches(vault, batches)
    return expired


def close_expired_batch_questions(core, expired_batch_ids: list[str]) -> int:
    """Mark the owner-inbox questions of expired batches ``expired`` so the
    ~5-cap queue never accumulates stale broker slots (ing-02) and a LATE
    answer is refused at the inbox level too (``record_answer`` only touches
    ``open`` entries)."""
    if not expired_batch_ids:
        return 0
    keys = {BROKER_KEY_PREFIX + b for b in expired_batch_ids}
    entries = core._read_inbox()
    closed = 0
    for e in entries:
        if (isinstance(e, dict) and e.get("key") in keys
                and e.get("status", "open") == "open"):
            e["status"] = "expired"
            closed += 1
    if closed:
        core._write_inbox(entries)
    return closed


def parse_batch_answer(answer: str, batch_ids: list[str]) -> tuple[list[str] | None, str]:
    """Parse an owner answer against the batch's candidate ids.

    Returns ``(accepted_ids, outcome)``; ``accepted_ids is None`` means the
    answer was invalid (not consumable — candidates requeue)."""
    a = (answer or "").strip().lower()
    if a == _ACCEPT_ALL:
        return list(batch_ids), "accept-all"
    if a == _REJECT_ALL:
        return [], "reject-all"
    m = _ACCEPT_PARTIAL_RE.match(a)
    if m:
        ids = [s.strip() for s in m.group("ids").split(",") if s.strip()]
        unknown = [i for i in ids if i not in batch_ids]
        if unknown:
            return None, f"invalid-answer: not in batch: {', '.join(unknown)}"
        return ids, "accept-partial"
    return None, f"invalid-answer: unparseable {answer!r}"


def consume_answers(core, now: _dt.datetime | None = None) -> dict[str, Any]:
    """The ANSWER-CONSUMER: apply owner answers to broker questions ONLY.

    - Ignores every inbox entry outside the ``cosbroker:``/``coscorrect:``
      namespaces (an unrelated answered question is never consumed here).
    - Verifies the batch record's Ed25519 signature over its recomputed
      candidate-set digest before acting (a tampered batches.jsonl fails).
    - Enforces subset validation, one-shot consumption (a replayed answer to a
      consumed batch is rejected), and late-answer rejection (expired batch).
    - Moves ONLY accepted candidates into capture-inbox/ (whence the ordinary
      audited host drain signs them); rejected candidates go to rejected/.
    """
    from . import audit

    now = now or _utcnow()
    vault = core.vault
    report: dict[str, Any] = {
        "accepted": [], "rejected": [], "requeued": [],
        "replay_rejected": [], "late_rejected": [], "invalid": [],
        "corrections_applied": [], "corrections_discarded": [],
        "corrections_failed": [],
    }
    entries = core._read_inbox()
    answered = {e["key"]: e for e in entries
                if isinstance(e, dict) and e.get("status") == "answered"
                and isinstance(e.get("key"), str)}

    batches = _read_jsonl(_batches_path(vault))
    by_id = {b.get("batch_id"): b for b in batches}
    changed = False
    pending = proposals_dir(vault) / "pending"
    rej_dir = proposals_dir(vault) / "rejected"

    for key, entry in answered.items():
        if not key.startswith(BROKER_KEY_PREFIX):
            continue
        batch_id = key[len(BROKER_KEY_PREFIX):]
        b = by_id.get(batch_id)
        if b is None:
            report["invalid"].append({"batch_id": batch_id, "reason": "unknown-batch"})
            continue
        state = b.get("state")
        if state == "consumed":
            report["replay_rejected"].append(batch_id)
            continue
        if state == "expired":
            report["late_rejected"].append(batch_id)
            continue
        if state != "open":
            report["invalid"].append({"batch_id": batch_id, "reason": f"state={state}"})
            continue
        # Anti-tamper: recompute the digest from the stored candidate set and
        # verify the enqueue-time signature with the HOST public key.
        digest = batch_digest(batch_id, b.get("created", ""), b.get("candidates", []))
        sig_ok = digest == b.get("digest")
        if sig_ok:
            try:
                from cryptography.hazmat.primitives.serialization import load_pem_public_key
                pub = load_pem_public_key(audit.public_key_pem())
                pub.verify(bytes.fromhex(b.get("sig", "")), digest.encode("utf-8"))
            except Exception:
                sig_ok = False
        if not sig_ok:
            b["state"] = "invalid"
            b["consumed_at"] = _ts(now)
            changed = True
            report["invalid"].append({"batch_id": batch_id,
                                      "reason": "digest/signature verification failed"})
            continue

        batch_ids = [c["id"] for c in b.get("candidates", [])]
        accepted_ids, outcome = parse_batch_answer(entry.get("answer", ""), batch_ids)
        if accepted_ids is None:
            # Unconsumable answer: candidates stay pending (requeue into the
            # next batch); the batch closes so it can't be replayed forever.
            b["state"] = "consumed"
            b["outcome"] = outcome
            b["consumed_at"] = _ts(now)
            changed = True
            report["invalid"].append({"batch_id": batch_id, "reason": outcome})
            report["requeued"].extend(batch_ids)
            continue

        sha_by_id = {c["id"]: c["sha256"] for c in b.get("candidates", [])}
        for nid in batch_ids:
            src_md = pending / f"{nid}.md"
            src_meta = pending / f"{nid}.json"
            if nid in accepted_ids:
                ok = src_md.exists()
                body = ""
                if ok:
                    body = src_md.read_text(encoding="utf-8")
                    ok = sha256_text(body) == sha_by_id.get(nid)
                if not ok:
                    report["invalid"].append(
                        {"batch_id": batch_id, "id": nid,
                         "reason": "pending file missing or content drifted "
                                   "since batch digest — not promoted"})
                    continue
                meta, _ = frontmatter.parse_text(body)
                record_outcome(vault, pattern=meta.get("pattern"), ident=nid,
                               outcome="accepted",
                               bundle_version=meta.get("bundle_version"), ts=_ts(now))
                sign_as_note = True
                if meta.get("kind") == "commitment":
                    try:
                        sign_as_note = _spine_ingest_commitment(
                            vault, meta, source_ref=nid, now=now)
                    except Exception as exc:  # noqa: BLE001 — never block acceptance
                        report.setdefault("spine_errors", []).append(
                            {"id": nid, "reason": f"{type(exc).__name__}: {exc}"})
                if sign_as_note:
                    inbox_dir = config.capture_inbox_dir(vault)
                    inbox_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src_md), inbox_dir / f"{nid}.md")
                    report["accepted"].append(nid)
                else:
                    # SP-01 hybrid: a non-keeper commitment is recorded into the
                    # spine ledger only — it never becomes a signed brain note.
                    evdir = host_dir(vault) / "spine-evidence"
                    evdir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src_md), evdir / f"{nid}.md")
                    report.setdefault("accepted_spine_only", []).append(nid)
                src_meta.unlink(missing_ok=True)
            else:
                if src_md.exists():
                    body = src_md.read_text(encoding="utf-8")
                    meta, _ = frontmatter.parse_text(body)
                    record_outcome(vault, pattern=meta.get("pattern"), ident=nid,
                                   outcome="rejected",
                                   bundle_version=meta.get("bundle_version"), ts=_ts(now))
                    rej_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src_md),
                                rej_dir / f"{now.strftime('%Y%m%dT%H%M%S')}-{nid}.md")
                src_meta.unlink(missing_ok=True)
                report["rejected"].append(nid)
        b["state"] = "consumed"
        b["outcome"] = outcome
        b["answer_key"] = key
        b["consumed_at"] = _ts(now)
        changed = True

    if changed:
        _write_batches(vault, batches)

    # -- correction answers (coscorrect:<round>:<msg_key>) --------------------
    cor_pending = proposals_dir(vault) / "corrections-pending"
    if cor_pending.is_dir():
        for f in sorted(cor_pending.glob("correction-*.json")):
            try:
                payload = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                f.unlink(missing_ok=True)
                continue
            key = (f"{CORRECT_KEY_PREFIX}{payload.get('round')}:"
                   f"{payload.get('msg_key')}")
            entry = answered.get(key)
            if entry is None:
                continue  # still awaiting the owner
            if str(entry.get("answer", "")).strip().lower() == "apply":
                try:
                    record_correction(
                        vault, payload["round"], payload["msg_key"],
                        payload["corrected_bucket"], payload["corrected_tier"],
                        actor=f"owner-inbox:{key}", ts=_ts(now))
                    report["corrections_applied"].append(key)
                except ValueError as exc:
                    report["corrections_failed"].append(
                        {"key": key, "reason": str(exc)})
            else:
                report["corrections_discarded"].append(key)
            f.unlink(missing_ok=True)
    return report


def enqueue_correction_questions(core, now: _dt.datetime | None = None) -> list[str]:
    """One owner-inbox question per claimed-but-unasked correction drop."""
    now = now or _utcnow()
    vault = core.vault
    asked: list[str] = []
    cor_pending = proposals_dir(vault) / "corrections-pending"
    if not cor_pending.is_dir():
        return asked
    for f in sorted(cor_pending.glob("correction-*.json")):
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        key = f"{CORRECT_KEY_PREFIX}{payload.get('round')}:{payload.get('msg_key')}"
        q = {
            "key": key,
            "question": (f"Apply COS correction (round {payload.get('round')}, "
                         f"msg {payload.get('msg_key')}): bucket→"
                         f"{payload.get('corrected_bucket')}, tier→"
                         f"{payload.get('corrected_tier')}?"),
            "options": ["apply", "discard"],
            "default": "discard",
            "context": "a correction becomes a correction_events row only via "
                       "this human answer — the VM drop alone never writes it",
        }
        if core.enqueue_question(q, source="cos-broker:correction",
                                 today=now.date()):
            asked.append(key)
    return asked


# -- correction transport (host store of record) --------------------------------
def _corrections_conn(vault) -> sqlite3.Connection:
    p = corrections_db_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS correction_events ("
        " round INTEGER NOT NULL,"
        " msg_key TEXT NOT NULL,"
        " corrected_bucket TEXT NOT NULL,"
        " corrected_tier TEXT NOT NULL,"
        " ts TEXT NOT NULL,"
        " actor TEXT NOT NULL,"
        " PRIMARY KEY (round, msg_key))")
    config.secure_file_permissions(p)
    return conn


def known_ledger_keys(vault) -> set[tuple[int, str]] | None:
    """(round, msg_key) pairs from the VM's shadow-ledger drop, or ``None``
    when no ledger file exists at all (then every key is unknown)."""
    vdir = verdict_drop_dir(vault)
    files = sorted(vdir.glob("shadow-ledger*.jsonl")) if vdir.is_dir() else []
    if not files:
        return None
    keys: set[tuple[int, str]] = set()
    for f in files:
        for e in _read_jsonl(f):
            r, k = e.get("round"), e.get("msg_key")
            if isinstance(r, int) and isinstance(k, str):
                keys.add((r, k))
    return keys


def record_correction(vault, round_: int, msg_key: str, bucket: str, tier: str,
                      *, actor: str, ts: str | None = None) -> dict[str, Any]:
    """Append ONE correction event. Append-only (no update/delete path exists);
    rejects a duplicate (round, msg_key) and any key not present in the shadow
    ledger. ``actor`` records the HUMAN act this row is attributed to."""
    if not isinstance(round_, int):
        raise ValueError("round must be an integer")
    ledger = known_ledger_keys(vault)
    if ledger is None:
        raise ValueError("unknown key: no shadow ledger present in verdict-drop/ "
                         "— corrections must reference a ledgered (round, msg_key)")
    if (round_, msg_key) not in ledger:
        raise ValueError(f"unknown key: ({round_}, {msg_key!r}) is not in the shadow ledger")
    conn = _corrections_conn(vault)
    try:
        with conn:
            conn.execute(
                "INSERT INTO correction_events "
                "(round, msg_key, corrected_bucket, corrected_tier, ts, actor) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (round_, msg_key, bucket, tier, ts or _ts(), actor))
    except sqlite3.IntegrityError:
        raise ValueError(f"duplicate key: a correction for ({round_}, {msg_key!r}) "
                         "already exists (the store is append-only)") from None
    finally:
        conn.close()
    return {"round": round_, "msg_key": msg_key, "corrected_bucket": bucket,
            "corrected_tier": tier, "actor": actor}


def list_corrections(vault) -> list[dict[str, Any]]:
    if not corrections_db_path(vault).exists():
        return []
    conn = _corrections_conn(vault)
    try:
        rows = conn.execute(
            "SELECT round, msg_key, corrected_bucket, corrected_tier, ts, actor "
            "FROM correction_events ORDER BY ts").fetchall()
    finally:
        conn.close()
    cols = ("round", "msg_key", "corrected_bucket", "corrected_tier", "ts", "actor")
    return [dict(zip(cols, r)) for r in rows]


def shadow_ledger_entries(vault) -> list[dict[str, Any]]:
    """All verdict rows from the VM's shadow-ledger drop, deduped by
    (round, msg_key) — the last write wins (same-night re-run idempotency)."""
    vdir = verdict_drop_dir(vault)
    files = sorted(vdir.glob("shadow-ledger*.jsonl")) if vdir.is_dir() else []
    by_key: dict[tuple[int, str], dict[str, Any]] = {}
    for f in files:
        for e in _read_jsonl(f):
            r, k = e.get("round"), e.get("msg_key")
            if isinstance(r, int) and isinstance(k, str):
                by_key[(r, k)] = e
    return list(by_key.values())


def calibration_report(vault) -> dict[str, Any]:
    """Shadow-mode trust-gate report: calibration = reduce(verdicts,
    correction_events). A verdict is bucket-correct when no correction exists
    for its (round, msg_key) OR the correction only changed the tier.
    Rounds completed = distinct rounds present in the shadow ledger."""
    verdicts = shadow_ledger_entries(vault)
    corr = {(c["round"], c["msg_key"]): c for c in list_corrections(vault)}
    rounds: dict[int, dict[str, int]] = {}
    buckets: dict[str, dict[str, Any]] = {}
    for v in verdicts:
        r = int(v["round"])
        key = (r, v["msg_key"])
        b = str(v.get("bucket", "?")).lower()
        rr = rounds.setdefault(r, {"total": 0, "corrected": 0})
        bb = buckets.setdefault(b, {"predicted": 0, "bucket_correct": 0})
        rr["total"] += 1
        bb["predicted"] += 1
        c = corr.get(key)
        if c is not None:
            rr["corrected"] += 1
        if c is None or str(c["corrected_bucket"]).lower() == b:
            bb["bucket_correct"] += 1
    for s in buckets.values():
        s["precision"] = (round(s["bucket_correct"] / s["predicted"], 4)
                          if s["predicted"] else None)
    total = len(verdicts)
    bucket_correct = sum(s["bucket_correct"] for s in buckets.values())
    return {
        "rounds_completed": len(rounds),
        "rounds": {str(k): v for k, v in sorted(rounds.items())},
        "verdicts": total,
        "corrections": len(corr),
        "overall_bucket_precision": (round(bucket_correct / total, 4)
                                     if total else None),
        "per_bucket": buckets,
    }


# -- evidence signer -------------------------------------------------------------
def _canonical_manifest(manifest: dict[str, Any]) -> str:
    unsigned = {k: v for k, v in manifest.items() if k not in ("sig", "public_key_pem")}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"))


def source_ledger_hash(vault) -> str:
    """sha256 over the (sorted) shadow-ledger drop bytes, or ``"none"``."""
    vdir = verdict_drop_dir(vault)
    files = sorted(vdir.glob("shadow-ledger*.jsonl")) if vdir.is_dir() else []
    if not files:
        return "none"
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode("utf-8"))
        h.update(f.read_bytes())
    return h.hexdigest()


def sign_evidence(vault, *, bundle_version: str, model_version: str,
                  dataset_window: str, files: list[Path] | None = None,
                  snapshot_generation: Any = None, name: str = "evidence",
                  now: _dt.datetime | None = None) -> dict[str, Any]:
    """Write a trust-gate evidence bundle under ``host/evidence/`` with a
    SIGNED, versioned manifest binding bundle version, model version, snapshot
    generation, dataset window, and the source-ledger hash. HOST-only (the
    caller gates); fails closed without a signing key."""
    from . import audit
    from .snapshot import read_manifest

    now = now or _utcnow()
    if snapshot_generation is None:
        snap = read_manifest(config.snapshot_dir(vault))
        snapshot_generation = getattr(snap, "generation", None)
    dest = evidence_dir(vault) / f"{safe_slug(name)}-{now.strftime('%Y%m%dT%H%M%SZ')}"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dest, 0o700)  # nosemgrep: insecure-file-permissions -- intentionally OWNER-ONLY (evidence dir), not overly-permissive
    except OSError:
        pass
    file_hashes: dict[str, str] = {}
    for f in files or []:
        f = Path(f)
        data = f.read_bytes()
        shutil.copy2(f, dest / f.name)
        file_hashes[f.name] = hashlib.sha256(data).hexdigest()
    manifest: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "bundle_version": bundle_version,
        "model_version": model_version,
        "snapshot_generation": snapshot_generation,
        "dataset_window": dataset_window,
        "source_ledger_hash": source_ledger_hash(vault),
        "created": _ts(now),
        "files": file_hashes,
    }
    key_obj, source = audit.resolve_signing_key()  # KeyUnavailable → fail closed
    manifest["sig"] = key_obj.sign(_canonical_manifest(manifest).encode("utf-8")).hex()
    manifest["public_key_pem"] = audit.public_key_pem().decode("ascii")
    (dest / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    config.secure_file_permissions(dest / "manifest.json")
    return {"dir": str(dest), "manifest": str(dest / "manifest.json"),
            "signed_with": source, "snapshot_generation": snapshot_generation}


def verify_evidence(bundle_dir: Path | str) -> dict[str, Any]:
    """Verify an evidence bundle: manifest signature (against the HOST key —
    never the manifest's own embedded key) + every payload file hash. A
    stale/edited JSON or payload fails."""
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    from . import audit

    bundle_dir = Path(bundle_dir)
    errors: list[str] = []
    mpath = bundle_dir / "manifest.json"
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "errors": [f"manifest unreadable: {exc}"]}
    try:
        pub = load_pem_public_key(audit.public_key_pem())
        pub.verify(bytes.fromhex(manifest.get("sig", "")),
                   _canonical_manifest(manifest).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — any failure = invalid signature
        errors.append(f"signature verification failed: {type(exc).__name__}: {exc}")
    if manifest.get("schema") != EVIDENCE_SCHEMA:
        errors.append(f"unexpected schema: {manifest.get('schema')!r}")
    for fname, expected in (manifest.get("files") or {}).items():
        fpath = bundle_dir / fname
        if not fpath.exists():
            errors.append(f"payload missing: {fname}")
            continue
        actual = hashlib.sha256(fpath.read_bytes()).hexdigest()
        if actual != expected:
            errors.append(f"payload hash mismatch: {fname}")
    return {"ok": not errors, "errors": errors,
            "manifest": {k: manifest.get(k) for k in
                         ("schema", "bundle_version", "model_version",
                          "snapshot_generation", "dataset_window",
                          "source_ledger_hash", "created")}}


# -- priority-map generator --------------------------------------------------------
_OVERRIDE_LINE_RE = re.compile(
    r"^\s*[-*]\s*(?P<id>[a-z0-9][a-z0-9-]*)\s*:\s*(?P<prio>high|normal|low|exclude)\s*$",
    re.IGNORECASE)


def load_priority_overrides(vault) -> dict[str, str]:
    """Owner overrides from the validated overlay ``cos/`` category: body list
    lines of the form ``- <note-id>: high|normal|low|exclude``."""
    from . import overlay as ov

    overrides: dict[str, str] = {}
    cos_cat = ov.overlay_dir(vault) / "cos"
    if not cos_cat.is_dir():
        return overrides
    for f in sorted(cos_cat.glob("*.md")):
        try:
            _meta, body = frontmatter.parse_text(f.read_text(encoding="utf-8"))
        except OSError:
            continue
        for line in body.splitlines():
            m = _OVERRIDE_LINE_RE.match(line)
            if m:
                overrides[m.group("id").lower()] = m.group("prio").lower()
    return overrides


def generate_priority_map(core, *, max_tier: str | None = None,
                          now: _dt.datetime | None = None) -> dict[str, Any]:
    """Generate ``shared/priority-map.md`` from ``type: person``/``company``
    notes via a HOST-produced filtered projection. Default tier policy: the
    FULL vault (host egress default, owner ruling 2026-07-10) — deliberately
    NOT capped to Internal; pass ``max_tier`` to narrow. Owner overrides come
    from the overlay ``cos/`` category. The output lists ids/titles/metadata
    only — never note bodies."""
    from . import classification as cls
    from . import egress

    now = now or _utcnow()
    tier = max_tier or cls.TIERS[-1]
    people = core.bases_query({"type": "person"}, k=1000)
    companies = core.bases_query({"type": "company"}, k=1000)
    gated_people, prep = egress.apply_gate(people, tier)
    gated_companies, crep = egress.apply_gate(companies, tier)
    overrides = load_priority_overrides(core.vault)

    def _bucketed(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        buckets: dict[str, list[dict[str, Any]]] = {"high": [], "normal": [], "low": []}
        for it in sorted(items, key=lambda x: str(x.get("updated") or ""), reverse=True):
            prio = overrides.get(str(it.get("id", "")).lower(), "normal")
            if prio == "exclude":
                continue
            buckets[prio].append(it)
        return buckets

    sections = {"People": _bucketed(gated_people),
                "Companies": _bucketed(gated_companies)}
    lines = [
        "<!-- GENERATED by `brain cos-priority-map` — do not hand-edit. -->",
        f"<!-- generated: {_ts(now)} max_tier: {tier} "
        f"overrides: {len(overrides)} -->",
        "# COS priority map", "",
    ]
    for heading, buckets in sections.items():
        lines.append(f"## {heading}")
        for prio in ("high", "normal", "low"):
            for it in buckets[prio]:
                lines.append(
                    f"- [[{it.get('id')}]] — {it.get('title') or it.get('id')} "
                    f"(priority: {prio}, {it.get('classification') or 'UNLABELLED'}, "
                    f"updated {it.get('updated') or '?'})")
        lines.append("")
    out_path = priority_map_path(core.vault)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    try:
        os.chmod(out_path, 0o644)  # VM-readable projection
    except OSError:
        pass
    return {"path": str(out_path), "max_tier": tier,
            "people": len(gated_people), "companies": len(gated_companies),
            "withheld": prep["withheld"] + crep["withheld"],
            "overrides": overrides}


# -- auto-capture hold store ---------------------------------------------------------
def hold_add(vault, content: str, *, not_before: str,
             ident: str | None = None) -> dict[str, Any]:
    """Park a qualifying auto-capture item UNSIGNED until ``not_before``.

    The item enters capture-inbox/ (and thence the signed drain) ONLY after
    the stated interval expires — the undo window. Cancellation before expiry
    is atomic (see ``hold_cancel``)."""
    from . import capture as cap_mod

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
    md.write_text(staged, encoding="utf-8")
    marker.write_text(json.dumps(
        {"id": nid, "not_before": _ts(nb), "created": _ts()},
        sort_keys=True) + "\n", encoding="utf-8")
    return {"id": nid, "not_before": _ts(nb), "path": str(md), "signed": False}


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
        nb = _parse_ts(m.get("not_before", ""))
        m["due"] = bool(nb and nb <= now)
        out.append(m)
    return out


def hold_cancel(vault, ident: str) -> bool:
    """Atomically cancel a held item. The claim is the RENAME of the hold
    marker — ``os.rename`` is atomic, so exactly one of cancel/release wins;
    the loser sees FileNotFoundError and reports False/skips."""
    nid = safe_slug(ident)
    hdir = hold_dir(vault)
    marker = hdir / f"{nid}.hold.json"
    claimed = hdir / f"{nid}.cancelled.json"
    try:
        os.rename(marker, claimed)
    except FileNotFoundError:
        return False  # already released or already cancelled — the race loser
    (hdir / f"{nid}.md").unlink(missing_ok=True)
    claimed.unlink(missing_ok=True)
    return True


def hold_release_due(vault, now: _dt.datetime | None = None) -> list[str]:
    """Move every DUE held item into capture-inbox/ (the signed drain path).

    Claim-by-rename per item (same atomic marker claim as ``hold_cancel``), so
    a concurrent cancel and release can never both act on one item."""
    now = now or _utcnow()
    released: list[str] = []
    hdir = hold_dir(vault)
    if not hdir.is_dir():
        return released
    for marker in sorted(hdir.glob("*.hold.json")):
        try:
            m = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        nb = _parse_ts(m.get("not_before", ""))
        if nb is None or nb > now:
            continue
        nid = m.get("id") or marker.name.replace(".hold.json", "")
        claimed = hdir / f"{nid}.releasing.json"
        try:
            os.rename(marker, claimed)
        except FileNotFoundError:
            continue  # a concurrent cancel/release won the claim
        md = hdir / f"{nid}.md"
        if md.exists():
            inbox_dir = config.capture_inbox_dir(vault)
            inbox_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(md), inbox_dir / f"{nid}.md")
            released.append(nid)
        claimed.unlink(missing_ok=True)
    return released


# -- ING-04: auto-capture criteria (pattern-level acceptance evidence) -----------
# A qualifying candidate is routed into the s0e hold store (above) instead of
# the owner-inbox batch — NOT straight to a signed note. It still sits
# UNSIGNED for ``undo_hours`` with a daily digest + one-word revert
# (``brain cos-hold cancel <id>``) before the ordinary hold-release drain ever
# signs it. This is the one IRREVERSIBLE step in the whole broker (a signed
# note joins the hash-chained audit brain; supersession retires but never
# removes it), so the bar is held deliberately higher than auto-archive:
#
#   - a documented MINIMUM VOLUME per pattern (1/1 = 100% is disqualified by
#     construction: the default floor is well above 1);
#   - ZERO claim-time classification/security defects for the pattern in the
#     evidence window;
#   - a Wilson-score LOWER BOUND on the accept rate (never the raw
#     percentage — a lower bound is conservative under small samples in a
#     way a raw ratio is not).
#
# Pattern taxonomy and ``bundle_version`` are OPAQUE strings supplied by the
# proposing skill (frontmatter ``pattern:``/``bundle_version:`` on the
# candidate) — this module never hardcodes what a "pattern" means. Evidence
# is scoped to the CURRENT bundle_version only (s07 version-binding rule): a
# freshly updated skill starts every pattern back at zero volume, never
# inheriting a prior version's history.
AUTOCAP_MIN_VOLUME_ENV = "BRAIN_COS_AUTOCAP_MIN_VOLUME"
DEFAULT_AUTOCAP_MIN_VOLUME = 8
AUTOCAP_MIN_LOWER_BOUND_ENV = "BRAIN_COS_AUTOCAP_MIN_LOWER_BOUND"
DEFAULT_AUTOCAP_MIN_LOWER_BOUND = 0.85
AUTOCAP_UNDO_HOURS_ENV = "BRAIN_COS_AUTOCAP_UNDO_HOURS"
DEFAULT_AUTOCAP_UNDO_HOURS = 24
_UNPATTERNED = {"", "unclassified", "unknown", None}


def autocap_config_path(vault=None) -> Path:
    return host_dir(vault) / "autocap-config.json"


def _autocap_defaults() -> dict[str, Any]:
    return {
        "min_volume": _env_int(AUTOCAP_MIN_VOLUME_ENV, DEFAULT_AUTOCAP_MIN_VOLUME),
        "min_lower_bound": _env_float(AUTOCAP_MIN_LOWER_BOUND_ENV,
                                      DEFAULT_AUTOCAP_MIN_LOWER_BOUND),
        "undo_hours": _env_int(AUTOCAP_UNDO_HOURS_ENV, DEFAULT_AUTOCAP_UNDO_HOURS),
    }


def _env_int(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env, default))
    except ValueError:
        return default


def _env_float(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except ValueError:
        return default


def load_autocap_config(vault=None) -> dict[str, Any]:
    """Owner-editable, HOST-only criteria store (never skill text — the
    'learned pattern thresholds live in cos-ops config' requirement). Missing
    file = pure env-var defaults for every pattern."""
    defaults = _autocap_defaults()
    p = autocap_config_path(vault)
    patterns: dict[str, Any] = {}
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            defaults.update({k: v for k, v in raw.items() if k in defaults})
            if isinstance(raw.get("patterns"), dict):
                patterns = raw["patterns"]
    return {"defaults": defaults, "patterns": patterns}


def _pattern_config(vault, pattern: str) -> dict[str, Any]:
    cfg = load_autocap_config(vault)
    out = dict(cfg["defaults"])
    out.update(cfg["patterns"].get(pattern, {}) if isinstance(cfg["patterns"], dict) else {})
    return out


def _outcomes_path(vault=None) -> Path:
    return proposals_dir(vault) / "outcomes.jsonl"


def record_outcome(vault, *, pattern: str, ident: str, outcome: str,
                   bundle_version: str, ts: str | None = None) -> None:
    """Append ONE owner-decision or claim-time-defect record. Never mutated,
    never deleted (the acceptance evidence this gate reads is itself
    audit-shaped, even though it lives outside the signed note chain)."""
    _append_jsonl(_outcomes_path(vault), {
        "pattern": pattern or "unclassified", "id": ident, "outcome": outcome,
        "bundle_version": bundle_version or "unknown", "ts": ts or _ts(),
    })


def _wilson_lower_bound(successes: int, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    phat = successes / n
    denom = 1 + z * z / n
    center = phat + z * z / (2 * n)
    margin = z * ((phat * (1 - phat) + z * z / (4 * n)) / n) ** 0.5
    return max(0.0, (center - margin) / denom)


def pattern_stats(vault, pattern: str, bundle_version: str) -> dict[str, Any]:
    """Owner-decision volume/accept-rate + claim-time defect count for
    ``pattern``, scoped to THIS ``bundle_version`` only."""
    n = accepted = defects = 0
    for e in _read_jsonl(_outcomes_path(vault)):
        if e.get("pattern") != pattern or e.get("bundle_version") != bundle_version:
            continue
        outcome = e.get("outcome")
        if outcome == "accepted":
            n += 1
            accepted += 1
        elif outcome == "rejected":
            n += 1
        elif outcome == "claim-rejected-security":
            defects += 1
    return {"n": n, "accepted": accepted, "defects": defects,
            "lower_bound": _wilson_lower_bound(accepted, n)}


def auto_capture_eligible(vault, pattern: str | None,
                          bundle_version: str | None) -> tuple[bool, dict[str, Any]]:
    """The ING-04 gate. Returns ``(eligible, stats)`` — ``stats`` always
    carries enough to explain the decision (never a bare bool)."""
    if pattern in _UNPATTERNED or bundle_version in _UNPATTERNED:
        return False, {"reason": "no pattern/bundle_version on candidate"}
    cfg = _pattern_config(vault, pattern)
    stats = pattern_stats(vault, pattern, bundle_version)
    stats["config"] = cfg
    if stats["n"] < cfg["min_volume"]:
        return False, {**stats, "reason": "below-min-volume"}
    if stats["defects"] > 0:
        return False, {**stats, "reason": "defects-present"}
    if stats["lower_bound"] < cfg["min_lower_bound"]:
        return False, {**stats, "reason": "lower-bound-below-threshold"}
    return True, {**stats, "reason": "eligible"}


# -- SP-01: commitment spine ingestion (hybrid capture, decision 1) -------------
# A commitment-kind candidate ALWAYS gets a spine ledger row on acceptance.
# Only a "keeper" (P0/P1-equivalent counterparty — reusing the existing
# priority-map high/normal/low vocabulary, §`load_priority_overrides` — with
# a due date at least a week out) is ALSO signed as a full brain note; every
# other accepted commitment stays spine-only (never bloats the vault with a
# note per small ask). Expected candidate frontmatter: ``kind: commitment``,
# ``direction: owed_by_me|owed_to_me``, ``counterparty``, ``due`` (ISO,
# optional), ``topic`` (optional — falls back to a slug of the body).
KEEPER_HORIZON_DAYS_ENV = "BRAIN_COS_KEEPER_HORIZON_DAYS"
DEFAULT_KEEPER_HORIZON_DAYS = 7


def _is_keeper_counterparty(vault, counterparty: str | None) -> bool:
    if not counterparty:
        return False
    overrides = load_priority_overrides(vault)
    return overrides.get(str(counterparty).lower()) == "high"


def _spine_ingest_commitment(vault, meta: dict[str, Any], *, source_ref: str,
                             now: _dt.datetime) -> bool:
    """Record ONE accepted commitment candidate into the spine. Returns
    whether it also qualifies to be signed as a brain note (keeper)."""
    from . import spine as spine_mod

    direction = meta.get("direction") or "owed_by_me"
    if direction not in spine_mod.DIRECTIONS:
        direction = "owed_by_me"
    counterparty = str(meta.get("counterparty") or meta.get("title") or "unknown")
    text = str(meta.get("text") or meta.get("title") or source_ref)
    due = meta.get("due")
    topic = meta.get("topic")
    spine_mod.record_event(vault, event="created", direction=direction,
                           counterparty=counterparty, text=text, topic=topic,
                           due=due, source_ref=source_ref, ts=_ts(now))
    due_dt = _parse_ts(due) if due else None
    horizon = _env_days(KEEPER_HORIZON_DAYS_ENV, DEFAULT_KEEPER_HORIZON_DAYS)
    horizon_ok = bool(due_dt and (due_dt - now).days >= horizon)
    return _is_keeper_counterparty(vault, counterparty) and horizon_ok


def auto_capture_fold(vault, now: _dt.datetime | None = None) -> dict[str, Any]:
    """Route every currently-PENDING proposal whose pattern is auto-capture
    eligible into the hold store (undo-window gated — see the hold store
    above), instead of the next owner-inbox batch. Runs BEFORE
    ``enqueue_batch`` in the broker fold so only non-qualifying candidates
    ever reach the owner. Never signs anything itself."""
    now = now or _utcnow()
    held: list[dict[str, Any]] = []
    pending = proposals_dir(vault) / "pending"
    for m in _pending_metas(vault):
        nid = m.get("id")
        md = pending / f"{nid}.md"
        if not md.exists():
            continue
        try:
            content = md.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, _ = frontmatter.parse_text(content)
        pattern = meta.get("pattern")
        bundle_version = meta.get("bundle_version")
        eligible, stats = auto_capture_eligible(vault, pattern, bundle_version)
        if not eligible:
            continue
        cfg = stats.get("config", _autocap_defaults())
        not_before = _ts(now + _dt.timedelta(hours=cfg.get("undo_hours",
                                                            DEFAULT_AUTOCAP_UNDO_HOURS)))
        try:
            hold_add(vault, content, not_before=not_before, ident=nid)
        except ValueError:
            continue  # a hold already exists for this id — leave it pending
        (pending / f"{nid}.json").unlink(missing_ok=True)
        md.unlink(missing_ok=True)
        record_outcome(vault, pattern=pattern, ident=nid, outcome="auto-captured",
                       bundle_version=bundle_version, ts=_ts(now))
        held.append({"id": nid, "pattern": pattern, "not_before": not_before,
                     "stats": {k: stats[k] for k in ("n", "accepted", "lower_bound")}})
    return {"held": held}


# -- ingest sweeper (host-broker) ------------------------------------------------
# The Cowork VM has no view of the HOST's ~/Downloads (browser downloads land
# there), so the VM writes an ingest MANIFEST line per triggered download into
# the VM-writable drop; the host sweeper matches each named file in the host
# downloads dir and moves it into <vault>/inbox/ (the ordinary signed-ingest
# drop zone — quarantine of unknown extensions per ADR-0003 stays downstream).
INGEST_SWEEP_MAX_BYTES_ENV = "BRAIN_COS_SWEEP_MAX_BYTES"
DEFAULT_INGEST_SWEEP_MAX_BYTES = 200 * 1024 * 1024
INGEST_SWEEP_DOWNLOADS_ENV = "BRAIN_COS_DOWNLOADS_DIR"
INGEST_SWEEP_SKEW_SECONDS = 300          # manifest ts vs file mtime clock skew
INGEST_SWEEP_SIZE_TOLERANCE = 0.10       # when the manifest carries a size


def ingest_manifest_dir(vault=None) -> Path:
    return drop_dir(vault) / "ingest-manifest"


def _sweep_claims_path(vault) -> Path:
    return ingest_manifest_dir(vault) / "claims.jsonl"


def _manifest_line_key(entry: dict[str, Any]) -> str:
    """Stable identity of ONE manifest line (idempotency key for claims)."""
    return sha256_text(json.dumps(entry, sort_keys=True, separators=(",", ":")))


def _sweep_max_bytes() -> int:
    try:
        return int(os.environ.get(INGEST_SWEEP_MAX_BYTES_ENV,
                                  DEFAULT_INGEST_SWEEP_MAX_BYTES))
    except ValueError:
        return DEFAULT_INGEST_SWEEP_MAX_BYTES


def _unique_dest(inbox: Path, filename: str) -> Path:
    dest = inbox / filename
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    for i in range(2, 1000):
        cand = inbox / f"{stem}-{i}{suffix}"
        if not cand.exists():
            return cand
    raise ValueError(f"cannot uniquify destination for {filename!r}")


def ingest_sweep(vault, *, downloads_dir: Path | str | None = None,
                 dry_run: bool = False,
                 now: _dt.datetime | None = None) -> dict[str, Any]:
    """HOST sweeper: claim unclaimed ingest-manifest lines against the host
    downloads dir and MOVE exact-filename matches into ``<vault>/inbox/``.

    Safety contract:
    - filenames are basename-only — any path separator / ``..`` is refused;
    - a symlinked candidate (or manifest file) is refused, never followed;
    - files above the size cap (default 200 MB) are refused;
    - a match requires exact filename, size within tolerance when the
      manifest carries one, and mtime newer than the manifest ts minus skew;
    - claims are append-only, one per manifest line — a re-run never
      double-moves (idempotent);
    - NOTHING the manifest does not name is ever touched, moved, or deleted.
    """
    now = now or _utcnow()
    ddir = Path(downloads_dir
                or os.environ.get(INGEST_SWEEP_DOWNLOADS_ENV)
                or (Path.home() / "Downloads"))
    mdir = ingest_manifest_dir(vault)
    inbox = config.vault_root(vault) / "inbox"
    report: dict[str, Any] = {"downloads_dir": str(ddir), "dry_run": dry_run,
                              "moved": [], "refused": [], "unmatched": [],
                              "already_claimed": 0}
    if not mdir.is_dir():
        return report
    claims = _read_jsonl(_sweep_claims_path(vault))
    claimed_keys = {c.get("key") for c in claims}
    max_bytes = _sweep_max_bytes()

    def _claim(key: str, entry: dict[str, Any], disposition: str,
               dest: str | None = None) -> None:
        if dry_run:
            return
        rec = {"key": key, "msg_key": entry.get("msg_key"),
               "filename": entry.get("filename"), "disposition": disposition,
               "ts": _ts(now)}
        if dest:
            rec["dest"] = dest
        _append_jsonl(_sweep_claims_path(vault), rec)
        claimed_keys.add(key)

    for mf in sorted(mdir.glob("manifest-*.jsonl")):
        # same trust boundary as claim_drops: the drop dir is VM-writable —
        # never follow a symlinked manifest.
        if mf.is_symlink() or not mf.is_file():
            continue
        for entry in _read_jsonl(mf):
            key = _manifest_line_key(entry)
            if key in claimed_keys:
                report["already_claimed"] += 1
                continue
            # field-run finding 2026-07-14: the kernel writes richer manifests
            # (expected_filename = the EML it downloaded, attachment_filename =
            # the attachment inside) — accept any of the names it offers.
            names = [entry.get(k) for k in
                     ("filename", "expected_filename", "attachment_filename")]
            names = [n for n in names if isinstance(n, str) and n.strip()]
            safe = [n for n in names
                    if n == os.path.basename(n) and n not in (".", "..")]
            if not safe:
                _claim(key, entry, "refused: unsafe filename (basename only)")
                report["refused"].append({"filename": names[0] if names else None,
                                          "reason": "unsafe filename"})
                continue
            fname = next((n for n in safe if (ddir / n).exists()), None)
            if fname is None:
                report["unmatched"].extend(safe)  # unclaimed — retry next run
                continue
            cand = ddir / fname
            if cand.is_symlink() or not cand.is_file():
                _claim(key, entry, "refused: symlink / not a regular file")
                report["refused"].append({"filename": fname,
                                          "reason": "symlink refused"})
                continue
            size = cand.stat().st_size
            if size > max_bytes:
                _claim(key, entry, f"refused: size {size} > cap {max_bytes}")
                report["refused"].append({"filename": fname,
                                          "reason": "over size cap"})
                continue
            want = entry.get("approx_size_bytes")
            if isinstance(want, int) and want > 0:
                if abs(size - want) > max(want * INGEST_SWEEP_SIZE_TOLERANCE, 4096):
                    report["unmatched"].append(fname)  # wrong file — leave it
                    continue
            ets = _parse_ts(str(entry.get("ts", "")))
            if ets is not None and cand.stat().st_mtime < (
                    ets.timestamp() - INGEST_SWEEP_SKEW_SECONDS):
                report["unmatched"].append(fname)  # older than the download
                continue
            dest = _unique_dest(inbox, fname)
            if not dry_run:
                inbox.mkdir(parents=True, exist_ok=True)
                shutil.move(str(cand), dest)
            _claim(key, entry, "moved", dest=str(dest))
            report["moved"].append({"filename": fname, "dest": str(dest),
                                    "msg_key": entry.get("msg_key")})
    return report


# -- status summary --------------------------------------------------------------
def status_block(vault, role: str) -> dict[str, Any]:
    """Cheap counts for ``brain status --json``. The VM view only reads the
    zones it may touch (drop/ + shared/); host/ counts are host-only."""
    out: dict[str, Any] = {
        "ops_dir": str(ops_dir(vault)),
        "zones": {"host_private": str(host_dir(vault)),
                  "vm_readable": str(shared_dir(vault)),
                  "vm_writable": str(drop_dir(vault))},
    }
    try:
        pdir = proposal_drop_dir(vault)
        out["proposal_drops"] = len(list(pdir.glob("*.md"))) if pdir.is_dir() else 0
        out["priority_map_present"] = priority_map_path(vault).exists()
        if role == "host":
            out["pending_proposals"] = len(_pending_metas(vault))
            out["open_batches"] = len(open_batches(vault))
            holds = hold_list(vault)
            out["holds"] = len(holds)
            # ING-04 daily digest: id + not_before only (never content) so a
            # pending auto-capture is never silent — revert with
            # `brain cos-hold cancel <id>` before it releases.
            out["holds_pending"] = [
                {"id": h.get("id"), "not_before": h.get("not_before")}
                for h in holds]
            out["corrections"] = len(list_corrections(vault))
            try:
                from . import spine as spine_mod
                rep = spine_mod.radar(vault)
                out["spine"] = {"late": len(rep["late"]), "at_risk": len(rep["at_risk"]),
                                "open": len(spine_mod.list_all(vault, status="open"))}
            except Exception:  # noqa: BLE001 — spine status is best-effort
                out["spine"] = {"error": "unavailable"}
    except Exception as exc:  # noqa: BLE001 — status must never crash on cos state
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out
