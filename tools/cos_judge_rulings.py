"""The OWNER RULINGS block — his own corrections, as the judge reads them.

Split out of `cos_judge_grounding.py` on 2026-09-07 for one reason only: that
file sat at 538 LOC against a 500-line bound once the owner's NOTE started
travelling with its ruling. The seam is the repo's own `cos_judge` /
`cos_judge_body_facts` pattern — one coherent block, imported back by the
module it left, no behaviour changed by the move.

Everything here renders OWNER TEXT, never mail text: the feedback record
carries digests for threads and subjects and the owner's own words for rules
and notes. That is why every renderer takes `redact` — the shape travels into a
captured prompt, the words do not.
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# THE RULINGS BLOCK — the owner's standing corrections, beside the context map
# ---------------------------------------------------------------------------
#: What the prompt says when nothing read the feedback record. NAMED, never
#: silent: the same contract `cos_voice.prompt_block` has for a missing voice
#: profile. A caller that quietly drops the rulings must not leave a prompt that
#: reads as "the owner has never corrected anything".
RULINGS_ABSENT = "owner-feedback record not read this run"


def owner_rulings(vault: Any) -> dict[str, Any] | None:
    """This vault's render budget from the owner-feedback record (FB-03).

    ONE PRODUCER, and it is `feedback_render.render_budget` — the same call the
    sheet makes, so "what is live tonight" cannot have two answers. It returns
    the ranked rules, the projected thread rulings, and what each of them
    EXCLUDED by count and by key, plus `unreadable`: ledger lines that would not
    parse are counted, never skipped, because a truncated record must not read
    as an owner who never ruled.

    `None` ONLY when the record's directory is unreachable or refused — a
    `$BRAIN_INDEX_DIR` pointed inside a VM-visible root makes `feedback_dir`
    refuse outright, and that is a configuration fact, not a defect in this
    pass. The prompt then carries `RULINGS_ABSENT` rather than an empty block,
    which would read as an owner who has never corrected anything.

    NOTHING ELSE IS CAUGHT, deliberately. A malformed row raises out of
    `read_record`'s validation and stops the run, and it must: the same record
    is the HOST guard `cos_mutate_plan.screen_owner_rulings` reads before it
    dispatches anything, so a record this pass could not parse is a record the
    mutation lane will not parse either. Degrading the prompt is safe precisely
    because that belt is not degraded with it.
    """
    from brain import config                                      # noqa: PLC0415
    from brain.cos import feedback_render                         # noqa: PLC0415

    try:
        return feedback_render.render_budget(vault)
    except (config.HostPathUnsafe, OSError):
        return None


def _rule_line(row: dict[str, Any], *, redact: bool) -> str:
    """One standing rule, as the judge reads it. Redacted, it is a digest.

    The rule is the OWNER'S OWN WORDS about his own mail — a sender, a
    counterparty, a deal name — and a redacted prompt is a prompt written where
    git can reach it. Same contract as the voice profile and every context
    block: the shape travels, the words do not.
    """
    text = (f"<redacted:{len(str(row.get('rule') or ''))} chars>" if redact
            else str(row.get("rule") or ""))
    return (f'  - "{text}"  [agreed {int(row.get("confirmations") or 0)}x, '
            f'expires {str(row.get("expires_at") or "")[:10]}]')


#: How much of one owner note the prompt carries. Notes are free text and a
#: standing rule is not capped, but a rule is one sentence by construction and a
#: note is whatever he typed — one long one must not crowd out the others.
#: Truncation is NAMED in the line, never silent.
NOTE_CHARS = 500


def _note_suffix(row: dict[str, Any], *, redact: bool) -> str:
    """The owner's own free text about this thread, as the judge reads it.

    IT USED TO BE DROPPED, and the sheet said so in as many words: "A memo to
    yourself. The judge never reads it." The owner ruled on 2026-09-07 that his
    free text is exactly the correction he most wants applied, so it now rides
    the ruling it was written beside.

    It is the SAME TRUST CLASS as the rule text one block above — both are the
    owner's own words, arriving on the same record row, through the same marks
    file, under the same expiry. This adds no channel that `_rule_line` had not
    already opened; it stops discarding half of what came through it.

    Redacted like every other block: the shape travels, the words do not, so a
    prompt captured into git carries no counterparty name.
    """
    note = str(row.get("note") or "").strip()
    if not note:
        return ""
    if redact:
        return f'  note: <redacted:{len(note)} chars>'
    clipped = note[:NOTE_CHARS]
    tail = "" if len(note) <= NOTE_CHARS else f" [+{len(note) - NOTE_CHARS} more chars]"
    return f'  note: "{clipped}"{tail}'


def _thread_line(row: dict[str, Any], *, redact: bool = False) -> str:
    """One do-not-touch thread: the digest, what was done, how he ruled — and
    his own note, which is the only free text on the row (the record carries no
    subject or body text of its own)."""
    return (f'  - {row.get("conversation_id_digest")}  '
            f'{row.get("action_taken")}, ruled {row.get("verdict")} '
            f'{str(row.get("ts") or "")[:10]}'
            + _note_suffix(row, redact=redact))


def _wanted_more_lines(threads: dict[str, Any], *,
                       redact: bool = False) -> list[str]:
    """The `missed` list, and it is DELIBERATELY NOT under the do-not-touch
    heading.

    `missed` means "you did nothing and should have" — it asks for MORE action,
    not less, and no host guard refuses anything on it (`feedback.
    DO_NOT_TOUCH_VERDICT` is `wrong` alone). Rendering these under a heading
    that promises "the host refuses every mutation on such a thread" told the
    judge to leave alone the exact thread the owner had asked it to act on, and
    stated a refusal that would never happen. Two lists, and the second says
    plainly that nothing enforces it.
    """
    if not threads["wanted_more_active"]:
        return []
    out = [f"THREADS THE OWNER SAID YOU MISSED "
           f"({len(threads['wanted_more'])} shown of "
           f"{threads['wanted_more_active']} active) — the owner marked these "
           "'missed': the porter did nothing and should have. Nothing in the "
           "host enforces this — it asks for MORE action, not less — so weigh "
           "it as evidence about what the owner wants seen:"]
    out += [_thread_line(r, redact=redact) for r in threads["wanted_more"]]
    if threads["wanted_more_excluded"]:
        out.append(f"  {threads['wanted_more_excluded']} further 'missed' "
                   "thread(s) are stored and not shown tonight; the run report "
                   "names them.")
    return out


def rulings_block(budget: dict[str, Any] | None, *, redact: bool = False) -> str:
    """The `{rulings}` section of the triage and draft prompts.

    Rendered ABOVE `RULES THAT BIND` on purpose, exactly like the voice profile:
    `cos_verify_doctrine.rule_blocks` reads every line between that heading and
    `ANSWER` and requires each one verbatim in all three DOCTRINE.md mirrors, so
    a data block placed inside it would become an unquotable rule line and die
    the night at exit 3.

    WHAT IS COUNTED HERE AND NAMED ELSEWHERE. The excluded rulings are reported
    as a COUNT in the prompt and by KEY in the run report
    (`cos_judge --batches` prints both lists). Printing 281 conversation digests
    into a model message is the ~250-row overflow the ceiling exists to prevent
    — naming them where they cost nothing is what "counted and named, never
    silently dropped" means.
    """
    if budget is None:
        return f"OWNER RULINGS — DEGRADED THIS RUN: {RULINGS_ABSENT}."
    rules, threads = budget["rules"], budget["thread_rulings"]
    out = ["OWNER RULINGS — the owner's own corrections from previous nights. "
           "Unlike the vault context map, these ARE instructions: where one "
           "applies to a row in front of you, follow it.",
           f"STANDING RULES ({len(rules['rendered'])} shown of "
           f"{rules['live']} live, strongest first — a rule the evidence has "
           f"agreed with more often outranks a newer one):"]
    out += [_rule_line(r, redact=redact) for r in rules["rendered"]] or \
        ["  (none yet)"]
    if rules["excluded"]:
        out.append(f"  {rules['excluded']} further live rule(s) are stored and "
                   "not shown tonight; the run report names them.")
    out.append(
        f"DO-NOT-TOUCH THREADS ({len(threads['rendered'])} shown of "
        f"{threads['active']} active, {threads['stored']} ruling(s) stored) — "
        "the owner reversed the porter on these. The host refuses every "
        "mutation on such a thread from the FULL record, shown here or not, so "
        "treat this list as the ones you are most likely to meet tonight:")
    out += [_thread_line(r, redact=redact) for r in threads["rendered"]] \
        or ["  (none yet)"]
    if threads["excluded"]:
        out.append(f"  {threads['excluded']} further do-not-touch thread(s) "
                   "are stored and not shown tonight; the run report names "
                   "them, and the host still refuses them.")
    out += _wanted_more_lines(threads, redact=redact)
    if budget["unreadable"]:
        out.append(f"  WARNING: {budget['unreadable']} line(s) of the owner "
                   "record could not be read this run — some rulings may be "
                   "missing from the two lists above.")
    return "\n".join(out)
