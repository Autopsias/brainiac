"""The COS owner-feedback record — ONE ledger, TWO row kinds (FB-01).

`<vault>/.brain/cos/feedback.jsonl` is where every owner ruling lands, whether
it came from the morning sheet or from the owner moving a thread in Outlook.
It is HOST-ONLY: `.brain/` is gitignored wholesale and never indexed
(ADR-0003 Ruling 4), so nothing here can leak through `search`/`get`/`recent`.

Two kinds, and the second is not a variant of the first — it exists because
the first CANNOT carry the commonest correction:

* a **rule** row is a normalized standing ruling the judge quotes ("sender X
  digests are noise"). Deduplicated BY RULE, capped, and expired.
* a **thread ruling** row is keyed on `conversation_id` ALONE and says
  do-not-touch. NEVER deduplicated, NEVER capped in the ledger, NEVER expired.

A sheet `wrong` mark and every outlook-diff row carry NO rule text. If the only
kind were the rule row, the owner would drag a thread back out of Archive, the
next run would judge it identically on identical evidence, and archive it
again — nightly, forever. That is the whole reason the second kind is here.

The record carries SUBJECT HASHES, never subject text and never bodies. The
sheet the owner reads carries display text (they have to recognise the thread);
the durable record does not. See `docs/cos-feedback-record.md`, which freezes
this shape for s05 (both pens + the prompt side), s07 (the sheet), s09 (the
week report) and s10 (acceptance).

Three sibling modules complete the freeze. `feedback_rows.py` holds the frozen
ROW SHAPE (vocabularies, key sets, `validate_row`), split out on 2026-09-05 at
the file-size bound and re-exported here so every existing import is unchanged.
The other two import this one and never the
reverse: `feedback_render.py` (how much of the record a single prompt may
carry) and `feedback_sheet.py` (the JSON the morning sheet embeds).
"""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._approval import (_host_private_base, _proven_off_mount,
                        approved_vault_identity)
from ._io import _append_jsonl, _read_nofollow
from ._layout import _parse_ts, _ts, _utcnow
from ._learning_config import _env_int
from .feedback_rows import *  # noqa: F401,F403
from .feedback_rows import (_SHA256_RE, _THREAD_KEY_SUB, _WS_RE,
                            _check_common, _check_rule, _check_thread,
                            _require)


# --------------------------------------------------------------------------
# where it lives
# --------------------------------------------------------------------------
#: The host-private directory name, beside `cos-approved` and
#: `cos-attachment-anchors`. See `feedback_dir` for why it is not on the mount.
_FEEDBACK_DIRNAME = "cos-feedback"


def feedback_dir(vault: Any = None) -> Path:
    """HOST-PRIVATE, and deliberately NOT `<vault>/.brain/cos/`.

    THE PLAN SPECIFIED `<vault>/.brain/cos/feedback.jsonl`, and that path is on
    the mount. MEASURED, not assumed: `config.vm_visible_roots` returns the
    vault root AND `<vault>/.brain`, and `config.proven_off_mount` REFUSES
    `<vault>/.brain/cos` outright. A Cowork VM session can therefore write it.

    What that record does is turn its own rows into STANDING OWNER RULINGS in
    the judge's and the drafter's prompts — text that decides what the nightly
    archives in a real mailbox. An untrusted leg able to author owner
    instructions is the trifecta this repo has now closed four times, and each
    time the answer was the same one: placement is the control. The approved
    queue (INT-01), the drift dispositions (INT-02), the attachment anchors
    (INT-04), the writer lock (INT-05) and the standing ingestion approval all
    live here for this reason — "no amount of checking at open time fixes that;
    not being reachable does."

    Keyed exactly like the approved queue: a host-controlled base plus
    `vault_slug8` of the RESOLVED VAULT PATH, the one identity the VM cannot
    rewrite (never `.brain/vault-id`, which is a plain file on the mount).
    `_proven_off_mount` then refuses the directory outright if a
    `$BRAIN_INDEX_DIR` override has put it back inside a VM-visible root.

    The SHEETS stay on the mount — see `sheets_dir`.
    """
    return _proven_off_mount(
        _host_private_base() / _FEEDBACK_DIRNAME / approved_vault_identity(vault),
        vault, what="the COS feedback record")


def feedback_path(vault: Any = None) -> Path:
    return feedback_dir(vault) / "feedback.jsonl"


def sheets_dir(vault: Any = None) -> Path:
    """`<vault>/.brain/cos/sheets/` — where s07's `<date>.html` sheets land.

    ON THE MOUNT, unlike the record above, and that split is the point. The
    sheet is a page the owner opens and a session publishes; it has to be
    reachable, and it authorises nothing by existing — every mark read back off
    one goes through `feedback_sheet.validate_sheet_state` and then through
    `record_rule`/`record_thread_ruling`, which is where the closed shape and
    the host-private ledger take over.

    Named HERE rather than in s07 because s06's out-of-band heartbeat reads
    this directory's newest mtime to decide the nightly has died, and it is
    written before `sheet.py` exists. Two spellings of the same path is exactly
    how a heartbeat ends up watching a directory nothing writes.
    """
    return config.brain_runtime_dir(vault) / "cos" / "sheets"


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------
def _expiry(now: _dt.datetime) -> str:
    """`now + the expiry window`. The window is read HERE, on the append that
    stamps it — `$BRAIN_COS_FEEDBACK_RULE_DAYS` is a documented override and a
    documented override nothing reads is a lie in the documentation."""
    return _ts(now + _dt.timedelta(
        days=_env_int(RULE_EXPIRY_DAYS_ENV, DEFAULT_RULE_EXPIRY_DAYS)))


def _subject_hash(subject: str, subject_sha256: str | None) -> str:
    """ONE decision, for both writers: hash the subject, or carry a digest.

    `subject_sha256` is the already-hashed subject and exists for one caller —
    a REVOKE, and the sheet-side rows that echo the record's own digest back
    (s05 extension 2). The sheet renders a standing rule from the record, which
    by then carries only the digest; the subject TEXT is long gone, because
    this record never stored it. Passing `subject=""` on the way back would
    stamp the row with `sha256("")` and quietly claim the rule came off a
    thread with no subject. Hashing stays the DEFAULT and the only other path:
    give one or the other, never both, so two spellings of one fact cannot
    disagree.
    """
    _require(subject_sha256 is None or not subject,
             "give a subject OR a subject_sha256, never both")
    if subject_sha256 is None:
        return subject_digest(subject)
    _require(_SHA256_RE.fullmatch(str(subject_sha256)),
             f"subject_sha256 must be 64 hex chars, got {subject_sha256!r}")
    return str(subject_sha256)


def _prior_rule_counts(vault: Any, key: str) -> dict[str, int]:
    """The highest `confirmations`/`fired_count`/`contradicted_count` this
    `rule_key` has reached. ONE scan of the ledger for all three, because they
    are carried forward the same way and a second scan is a second answer."""
    out = {"confirmations": 0, "fired_count": 0, "contradicted_count": 0}
    if not key:
        return out
    for row in rule_rows(read_record(vault)["rows"]):
        if row.get("rule_key") != key:
            continue
        for field in out:
            out[field] = max(out[field], int(row.get(field) or 0))
    return out


def record_rule(vault: Any, *, source: str, conversation_id: str,
                action_taken: str, verdict: str, subject: str = "",
                subject_sha256: str | None = None,
                rule: str = "", note: str = "",
                revoked: bool = False, run: str = "",
                transport: str = "", sheet_id: str = "",
                rule_origin: str = "", fired: int = 0, contradicted: int = 0,
                confirm: bool = True,
                now: _dt.datetime | None = None) -> dict[str, Any]:
    """Append ONE rule row. `confirmations` and `expires_at` are computed here.

    `subject_sha256` IS THE ALREADY-HASHED SUBJECT, and it exists for exactly
    one caller: a REVOKE (s05 extension 2). The sheet renders a standing rule
    from the record, which by then carries only the digest — the subject TEXT is
    long gone, because this record never stored it. Passing `subject=""` on the
    way back would stamp the retirement row with `sha256("")` and quietly claim
    the rule came off a thread with no subject. Hashing stays the DEFAULT and
    the only other path: give one or the other, never both.

    A row whose `rule` repeats a rule already in the ledger is a CONFIRMATION:
    its `confirmations` is one past the highest this rule has reached, and its
    `expires_at` is pushed a fresh window out. That is the only producer of
    either field — `ranked_rules` reads them and nothing else writes them.

    `confirm=False` appends a row that carries the running counters forward
    WITHOUT claiming a confirmation — the contradiction path. `expires_at` is
    still refreshed, because the row is the rule's newest state and the fold
    reads the newest row; a contradiction that expired the rule on the spot
    would delete it rather than report it.

    A `revoked=True` row retires the rule: the fold takes the latest row per
    `rule_key`, so a revoke appended after a live rule removes it from every
    rendered block without rewriting a byte of history.

    CEILING: the count is read then appended, and `_append_jsonl` locks only
    the write. The nightly is single-writer by construction (the mutation lane
    holds an exclusive per-run lock), so two racing confirmations would cost
    one rank position and nothing else. Per-key CAS if that ever stops holding.
    """
    now = now or _utcnow()
    digest = _subject_hash(subject, subject_sha256)
    # Whitespace-collapsed, but NOT recased and NOT depunctuated: the judge and
    # the sheet quote this text verbatim, so it stays the owner's own words —
    # minus the stray padding a textarea leaves, which would otherwise render
    # into a prompt. `rule_key` does the aggressive folding.
    rule = _WS_RE.sub(" ", str(rule)).strip()
    key = rule_key(rule)
    prior = _prior_rule_counts(vault, key)
    # A REVOKE IS NOT A CONFIRMATION. It carries the count it retires (never
    # less than 1, which the field set requires), so re-minting the same rule
    # later starts from what the evidence actually reached rather than from a
    # counter the retirement inflated.
    # A CONTRADICTION IS NOT A CONFIRMATION EITHER, and that is what
    # `confirm=False` is for. A later mark that disagrees with a live rule has
    # to reach the rule's counters — the owner cannot decide to revoke one
    # without seeing how often it has been contradicted — but counting the
    # disagreement as agreement would push the rule UP the ranking and out
    # another 90 days, which is exactly backwards.
    count = (max(prior["confirmations"], 1) if revoked or not confirm
             else prior["confirmations"] + 1)
    row = {
        "kind": KIND_RULE, "schema": FEEDBACK_SCHEMA, "ts": _ts(now),
        "source": source,
        "thread": {"conversation_id": str(conversation_id),
                   "conversation_id_digest": thread_digest(conversation_id),
                   "subject_sha256": digest},
        "action_taken": action_taken, "verdict": verdict, "note": note,
        "rule": rule, "rule_key": key, "confirmations": count,
        "expires_at": _expiry(now), "revoked": bool(revoked), "run": str(run),
        "transport": str(transport), "sheet_id": str(sheet_id),
        "rule_origin": str(rule_origin),
        # CARRIED FORWARD AND ADDED TO, exactly like `confirmations`: the
        # ledger is append-only, so the newest row for a `rule_key` holds the
        # running totals and nothing rewrites an older one.
        "fired_count": prior["fired_count"] + max(int(fired), 0),
        "contradicted_count": (prior["contradicted_count"]
                               + max(int(contradicted), 0)),
    }
    return _append(vault, row)


def record_thread_ruling(vault: Any, *, source: str, conversation_id: str,
                         action_taken: str, verdict: str, subject: str = "",
                         subject_sha256: str | None = None,
                         note: str = "", run: str = "",
                         transport: str = "", sheet_id: str = "",
                         now: _dt.datetime | None = None) -> dict[str, Any]:
    """Append ONE do-not-touch ruling, keyed on `conversation_id` alone.

    No rule text, no expiry, no confirmation counter — by construction, not by
    convention (`THREAD_ROW_KEYS` refuses those keys). This is what the owner
    dragging a thread back out of Archive produces, and it must outlive every
    cap: one heavily-marked 361-thread sheet must not evict what an earlier
    night learned.
    """
    now = now or _utcnow()
    row = {
        "kind": KIND_THREAD, "schema": FEEDBACK_SCHEMA, "ts": _ts(now),
        "source": source, "conversation_id": str(conversation_id),
        "conversation_id_digest": thread_digest(conversation_id),
        "subject_sha256": _subject_hash(subject, subject_sha256),
        "action_taken": action_taken, "verdict": verdict, "note": note,
        "run": str(run), "transport": str(transport),
        "sheet_id": str(sheet_id),
    }
    return _append(vault, row)


def _append(vault: Any, row: dict[str, Any]) -> dict[str, Any]:
    validate_row(row)
    _append_jsonl(feedback_path(vault), row, vault=vault)
    return row


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------
def read_record(vault: Any = None) -> dict[str, Any]:
    """`{"path", "rows", "unreadable"}` — and `unreadable` is not decoration.

    A line that will not parse, or that parses and then fails `validate_row`,
    is COUNTED rather than skipped. Silently dropping it would turn a truncated
    or tampered ledger into "the owner never ruled on that", which is the same
    class of bug as accepting an unreadable body as absence of evidence. Every
    caller that renders rulings reports this number.
    """
    path = feedback_path(vault)
    out: dict[str, Any] = {"path": str(path), "rows": [], "unreadable": 0}
    if not path.exists():
        return out
    for line in _read_nofollow(path).decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            out["rows"].append(validate_row(row))
        except (ValueError, FeedbackRowInvalid):
            out["unreadable"] += 1
    return out


def rule_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("kind") == KIND_RULE]


def thread_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("kind") == KIND_THREAD]


__all__ = [
    'FEEDBACK_SCHEMA', 'SHEET_STATE_ELEMENT_ID', 'SHEET_STATE_SCHEMA',
    'KIND_RULE', 'KIND_THREAD', 'KINDS', 'SOURCES', 'VERDICTS',
    'DO_NOT_TOUCH_VERDICT', 'WANTED_MORE_VERDICT', 'ACTIONS',
    'ACTION_FOR_VERB', 'RULE_EXPIRY_DAYS_ENV', 'DEFAULT_RULE_EXPIRY_DAYS',
    'RULE_CAP_ENV', 'DEFAULT_RULE_CAP', 'THREAD_CEILING_ENV', 'TRANSPORTS',
    'DEFAULT_THREAD_RENDER_CEILING', 'RULE_ROW_KEYS', 'THREAD_ROW_KEYS',
    'FeedbackRowInvalid', 'feedback_dir', 'feedback_path', 'sheets_dir',
    'thread_digest', 'subject_digest', 'rule_key', 'validate_row',
    '_subject_hash', 'record_rule', 'record_thread_ruling', 'read_record', 'rule_rows',
    'thread_rows',
]
