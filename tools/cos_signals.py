#!/usr/bin/env python3
"""The five context facts the judgment rules read — produced by the HOST (GAP-04).

WHY THIS FILE EXISTS. `tools/cos_judge.py` reads five facts off its per-row
context — `unanswered_direct_ask`, `live_deadline`, `open_spine_commitment`,
`body_unreadable`, `screens_ran_unresolved` — and until this module nothing
anywhere WROTE any of them except a test fixture. Measured consequence
(`_evidence/cosv7/s09-prompt-validator-reconciliation.md`):
`triage.p3_act_needs_direct_ask` refused 100% of P3 `act` verdicts, every hold
category below `Held · flag` was unreachable, and the second clause of
`draft.response_warranted_scope` was dead. That is the `automated-mail-marker`
shape retired at run 127 and the run-135 lesson — a word with no producer is a
coin-flip night — five more times over.

THE RULE THIS FILE OBEYS: **the model may not certify its own input.** Every
value is computed here, from the run's own captured message data, and NOTHING
here reads a model answer. A boolean the judgment leg declares about itself is a
check grading its own homework; that is what run 127 had to retire.

THE INPUTS, both already on disk when the judge runs — no new extraction pass,
no second mailbox read: the DRIVER LEDGER row (`body_opened`, `body_chars`), and
the CAP-01 CAPTURE CORPUS row (`provenance.sender`, `.subject`, and for the <= 20
threads a night opens, the extracted `text` of the thread's NEWEST message).

TWO LEGS, AND THE WEAKER ONE IS LABELLED. A night reads ~235 threads and opens
20 bodies, so a body-only producer is blind on 90% of the ledger — and, measured,
on 100% of the rows `triage.p3_act_needs_direct_ask` actually refuses (every one
is `body_opened: False`). So the ask/deadline producers read the body when there
is one AND the SUBJECT LINE, a typed field INJ-03 permits and the same evidence
the judge was handed. `..._leg` records which fired.

CONSERVATIVE BY CONSTRUCTION. A false `unanswered_direct_ask` puts a thread in
the owner's action queue that does not belong there, so every detector is a
CLOSED marker list, never a general parser: a bare question mark is not an ask,
a `Re:` prefix is not an ask, a date without a deadline word is not a deadline,
and an ambiguous numeric date whose readings straddle the run date is refused.

WHAT THIS FILE CANNOT SEE, STATED. "Unanswered" is decided from what the OWNER'S
INBOX knows: the newest message in the thread is inbound, so nothing in the
thread answers it. An owner reply that exists only in Sent Items is invisible —
the driver enumerates `sentitems` but keeps only `{item_id, timestamp}` and drops
`ConversationId`, so the two cannot be joined. Carrying `ConversationId` on that
enumeration is the one change that would close it, and it needs a live run.
"""
from __future__ import annotations

import datetime as _dt
import re
import unicodedata
from typing import Any

#: Everything above the FIRST of these is the newest message's own words; below
#: it is the quoted chain, i.e. older messages that a later message may already
#: have answered. Matching the OWA/Outlook plain-text reply shapes the corpus
#: actually holds (EN/PT/ES/DE/NL headers, forward banners, `>` quoting).
_QUOTE_START = re.compile(
    r"(?mi)^\s*(?:From|De|Von|Van)\s*:\s"
    r"|^\s*Begin forwarded message\s*:"
    r"|^\s*-{2,}\s*(?:Original Message|Mensagem original|Mensaje original)"
    r"|^\s*_{10,}\s*$"
    r"|^\s*>",
)

#: Banner lines a tenant's mail gateway prepends to EVERY message. They are not
#: the sender's words and must never supply an ask marker.
_BANNER = re.compile(
    r"(?mi)^\s*(?:Internal|Interno|Confidential|Confidencial|Public|Público)"
    r"\s*[-–—]\s*\S+\s*$"
    r"|^\s*(?:Internal|Interno|External|Externo)\s*$"
    r"|^\s*CAUTION\s*:.*$",
)

#: The tenant's legal disclaimer. Boilerplate on every message, not the sender
#: speaking, and FULL of request language ("if you are not the intended
#: recipient, please inform the sender"). It sits inside the newest message's
#: own words on 7 of 151 distinct corpus texts and produced one false ask on the
#: first hand-check. Everything from its first line onward is cut.
_FOOTER = (
    "sigilo profissional", "aviso de confidencialidade", "aviso legal",
    "confidentiality notice", "intended only for", "intended recipient",
    "privileged and confidential", "apague-o", "delete this message",
    "considere o ambiente", "consider the environment", "disclaimer",
    "unsubscribe", "cancelar subscricao",
)

#: A DIRECT ASK is a REQUEST MADE OF THE READER, and the two legs need
#: different lists because a SUBJECT is a label and a BODY is prose. Matched
#: case-insensitively on NFKD-folded text, so `ação`/`acao` and `aprovação`/
#: `aprovacao` both hit, and always as whole words.
#:
#: Body markers carry a verb in request form or an explicit "please" — a bare
#: noun cannot be trusted in prose. Measured on run148's 20 opened threads: with
#: the noun `aprovacao` in the BODY list, two approval NOTIFICATIONS (about a
#: decision already taken) were called asks on the owner. In a SUBJECT the same
#: noun titles what is being asked for and all 9 hits were genuine.
_ASK_REQUEST = (
    # English
    "please approve", "please review", "please confirm", "please complete",
    "please advise", "please let us know", "please let me know", "please send",
    "please provide", "please find time", "could you", "can you please",
    "would you be able", "we need your", "needs your", "requires your",
    "let us know", "let me know", "awaiting your", "reply by", "respond by",
    "rsvp", "reminder to update", "reminder to complete",
    # Portuguese
    "por favor", "podes confirmar", "podias", "poderias", "poderia",
    "agradeco que", "agradecia que", "peco-te", "peco que",
    "pedir o teu apoio", "pedir o seu apoio", "preciso que", "necessito que",
    "aguardo a tua", "aguardo o teu", "aguarda a sua", "confirma se",
    "diz-me se", "avisa-me", "podes validar", "tens de validar", "carece de",
    # Spanish
    "tienes que validar", "podrias", "podria", "necesito que", "avisame",
    "confirmame", "quedo a la espera", "se requiere",
)

#: Label nouns — a SUBJECT LINE names what is wanted. Subject leg only.
_ASK_LABEL = (
    "action required", "action needed", "approval required", "approval needed",
    "pending approval", "your approval", "your sign-off", "sign-off",
    "response required", "request for approval",
    "acao necessaria", "acao requerida", "resposta necessaria",
    "aprovacao", "aprovacoes", "para aprovacao", "pedido de", "pedidos de",
    "confirmacao da", "confirmacao do",
    "accion requerida", "aprobacion", "solicitud de", "fecha limite para",
)

_ASK_SUBJECT = _ASK_REQUEST + _ASK_LABEL

#: A LIVE DEADLINE needs a deadline WORD as well as a date — a date on its own
#: is a meeting, an announcement or a version number.
#: Each entry is matched as WHOLE WORDS and must have a parseable date within
#: `_DEADLINE_REACH` characters, which is what lets short prepositions
#: ("ate", "hasta", "before") stay in the list without swallowing prose.
_DEADLINE_WORDS = (
    "deadline", "due", "no later than", "by close of", "respond by",
    "reply by", "rsvp", "expires", "expiry", "closing date", "submit by",
    "before end of", "before", "until",
    "prazo", "data limite", "ate", "responder ate", "entregar ate", "expira",
    "limite", "antes de",
    "plazo", "fecha limite", "hasta", "responder antes", "vence",
)

#: Mail-client CHROME the DOM read lane captures alongside — or instead of —
#: the message. STRIPPED before the body is judged, never evidence of emptiness
#: on its own: a first cut that called any of these "unreadable" fired on 64 of
#: 772 opened rows, and 62 were native-lane captures that DID carry the message
#: a few lines under the chrome. Only what is left after stripping decides.
_CHROME = (
    "select an item to read", "nothing is selected", "summarize this email",
    "this message is protected", "esta mensagem esta protegida",
    "rights-protected", "permission is required to view",
    "translate to english", "never translate from",
)

#: The Unicode private-use area — Outlook's toolbar icon font. Never content.
_PUA = re.compile("[\ue000-\uf8ff\U000f0000-\U000ffffd]+")

_MONTHS = {m: n for n, names in enumerate((
    ("january", "jan", "janeiro", "enero"), ("february", "feb", "fevereiro",
     "febrero"), ("march", "mar", "marco", "marzo"), ("april", "apr", "abril"),
    ("may", "maio", "mayo"), ("june", "jun", "junho", "junio"),
    ("july", "jul", "julho", "julio"), ("august", "aug", "agosto"),
    ("september", "sep", "sept", "setembro", "septiembre"),
    ("october", "oct", "outubro", "octubre"),
    ("november", "nov", "novembro", "noviembre"),
    ("december", "dec", "dezembro", "diciembre")), start=1) for m in names}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))

_ISO_DATE = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
_NUM_DATE = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](20\d{2})\b")
_NAMED_DATE = re.compile(
    rf"\b(\d{{1,2}})\s*(?:de\s+)?({_MONTH_RE})\b(?:\s+(?:de\s+)?(20\d{{2}}))?"
    rf"|\b({_MONTH_RE})\s+(\d{{1,2}})\b(?:,?\s*(20\d{{2}}))?", re.IGNORECASE)

#: How far from a deadline word a date may sit and still be ITS date.
_DEADLINE_REACH = 120
#: Topic tokens shorter than this discriminate nothing ("way", "de").
_TOPIC_MIN_TOKEN = 4


def fold(s: Any) -> str:
    """Accent-stripped, case-folded, whitespace-collapsed — the ONE
    normalization every marker match runs through, so `ação` and `acao` are the
    same word on both sides."""
    t = unicodedata.normalize("NFKD", str(s or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).casefold()


def newest_message_text(text: str | None) -> str:
    """The newest message's OWN words: everything above the quoted chain, with
    the gateway banners removed.

    This is the whole "answered" test. The corpus row holds the body of the
    NEWEST message in the thread; a reply reproduces the older messages BELOW
    itself, so an ask found above the first quoted header is by construction the
    last word anyone has said — nothing later in the thread answers it. An ask
    found in the quoted chain is an older ask that a later message may already
    have answered, which is why this function exists rather than scanning the
    whole body.
    """
    body = str(text or "")
    m = _QUOTE_START.search(body)
    if m:
        body = body[:m.start()]
    lines = _BANNER.sub("", body).split("\n")
    for i, line in enumerate(lines):
        if _has_marker(line, _FOOTER):
            lines = lines[:i]           # the disclaimer, and everything under it
            break
    return "\n".join(lines).strip()


#: WORD BOUNDARIES, NOT SUBSTRINGS. `"due" in "produced"` and `"ate" in "date"`
#: are both true, and the second alone put 32 spurious deadline hits into this
#: module's first measurement. Every marker matches as whole words.
_MARKER_RE: dict[str, re.Pattern[str]] = {}


def _marker_pattern(m: str) -> re.Pattern[str]:
    pat = _MARKER_RE.get(m)
    if pat is None:
        pat = _MARKER_RE[m] = re.compile(r"\b" + re.escape(fold(m)) + r"\b")
    return pat


def _has_marker(haystack: str, markers: tuple[str, ...]) -> str | None:
    h = fold(haystack)
    for m in markers:
        if _marker_pattern(m).search(h):
            return m
    return None


def _marker_offsets(haystack: str, markers: tuple[str, ...]) -> list[int]:
    h = fold(haystack)
    return [mo.start() for m in markers for mo in _marker_pattern(m).finditer(h)]


def _dates_in(text: str, default_year: int) -> list[tuple[int, Any]]:
    """`(offset, date)` per date token; a 2-tuple where a numeric form's
    day/month reading is ambiguous — refused, never guessed."""
    out: list[tuple[int, _dt.date | None]] = []
    for m in _ISO_DATE.finditer(text):
        y, mo, d = (int(x) for x in m.groups())
        out.append((m.start(), _safe_date(y, mo, d)))
    for m in _NUM_DATE.finditer(text):
        a, b, y = (int(x) for x in m.groups())
        first, second = _safe_date(y, b, a), _safe_date(y, a, b)
        if second is None:            # only one reading is a real date
            out.append((m.start(), first))
        elif first is None:
            out.append((m.start(), second))
        else:
            out.append((m.start(), (first, second)))   # type: ignore[arg-type]
    for m in _NAMED_DATE.finditer(text):
        d, mon, y1, mon2, d2, y2 = m.groups()
        if mon:
            day, month, year = int(d), _MONTHS[mon.casefold()], y1
        else:
            day, month, year = int(d2), _MONTHS[mon2.casefold()], y2
        out.append((m.start(), _safe_date(int(year) if year else default_year,
                                          month, day)))
    return out


def _safe_date(y: int, mo: int, d: int) -> _dt.date | None:
    try:
        return _dt.date(y, mo, d)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# the five facts
# ---------------------------------------------------------------------------
def unanswered_direct_ask(*, subject: str | None, new_text: str
                          ) -> tuple[bool, str | None]:
    """`(fact, leg)` — a direct ask, made of the owner, that nothing answers.

    MEANING: someone has asked the owner to do or decide something, and the last
    word in the thread is still that ask. DECIDED BY a closed request marker in
    the newest message's own words, else in the subject line. "Made of the
    owner" rests on the row being in the OWNER'S OWN INBOX (the driver
    enumerates `inbox` only) plus the marker being a request, not a statement;
    "unanswered" rests on the newest message being inbound.

    FAILURE MODE: a message the owner is only CC'd on, whose request is aimed at
    a named third party, reads as an ask on him — narrowed, not eliminated, by
    requiring a request marker rather than mere second person. And an owner
    reply sitting only in Sent Items is invisible (module docstring).
    """
    if _has_marker(new_text, _ASK_REQUEST):
        return True, "body"
    if _has_marker(subject or "", _ASK_SUBJECT):
        return True, "subject"
    return False, None


def live_deadline(*, subject: str | None, new_text: str, now: _dt.date
                  ) -> tuple[bool, str | None]:
    """`(fact, leg)` — a stated due date that has not passed as of the run.

    MEANING: the thread names a date by which something is owed and that date
    is still ahead. DECIDED BY a deadline WORD plus a parseable date within
    ``_DEADLINE_REACH`` characters of it, in the newest message's own words or
    the subject — a date with no deadline word is a meeting or an announcement.

    FAILURE MODE: an ambiguous numeric date (`08/09/2026`) has two readings; if
    they disagree about whether it has passed, the token is REFUSED, not
    guessed. A deadline in prose with no date ("by end of week") is missed — a
    false negative, costing a `Held · uncertain` label, not a missed action.
    """
    for leg, raw in (("body", new_text), ("subject", subject or "")):
        # BOTH SIDES ON THE FOLDED TEXT. `fold` collapses whitespace, so a
        # marker offset taken on folded text and a date offset taken on the raw
        # text are not in the same coordinate system and the reach test below
        # would compare nonsense.
        hay = fold(raw)
        spots = _marker_offsets(hay, _DEADLINE_WORDS)
        if not spots:
            continue
        for off, val in _dates_in(hay, now.year):
            if val is None:
                continue
            if not any(abs(off - s) <= _DEADLINE_REACH for s in spots):
                continue
            if isinstance(val, tuple):
                # Ambiguous d/m vs m/d: only usable when both readings agree.
                if (val[0] >= now) != (val[1] >= now):
                    continue
                val = val[0]
            if val >= now:
                return True, leg
    return False, None


def body_unreadable(*, body_opened: bool, body_chars: int, text: str | None,
                    subject: str | None = None,
                    extraction_error: Any = None) -> bool:
    """This run OPENED the body and got back nothing it can judge from.

    MEANING: the thread is held because the content could not be read — a
    rights-protected message, or an extraction that captured the mail client's
    chrome instead of the mail. DECIDED BY: the body was attempted
    (`body_opened`) and the result is an error, zero characters, or nothing
    beyond chrome and the row's own subject. A row the run never attempted is
    NOT unreadable — that is :func:`screens_ran_unresolved`.

    FAILURE MODE: the chrome list is closed, so a new client string survives
    stripping and reads as content until it is added. A genuinely short body
    ("Obrigado Alberto!") is real and is never called unreadable — length is
    never consulted.
    """
    if not body_opened:
        return False
    if extraction_error:
        return True
    if int(body_chars or 0) <= 0 or not str(text or "").strip():
        return True
    return not _content_beyond_chrome(text, subject)


def _content_beyond_chrome(text: Any, subject: Any) -> bool:
    """Is there anything in this body that is neither client chrome nor the
    row's own subject echoed back?

    The DOM read lane captures the reading pane, which begins with the SUBJECT
    and a row of Copilot/ribbon controls. A capture that got that far and no
    further read the mail client, not the mail — and it is indistinguishable
    from a real body by length alone, which is why the test is set membership
    against the subject rather than a character floor.
    """
    body = _PUA.sub(" ", str(text or ""))
    folded = fold(body)
    for c in _CHROME:
        folded = _marker_pattern(c).sub(" ", folded)
    return bool(set(_words(folded)) - set(_words(subject)))


def open_spine_commitment(*, sender: str | None, subject: str | None,
                          open_commitments: list[dict[str, Any]]) -> bool:
    """An OPEN commitment on the spine names this counterparty AND this topic.

    MEANING: the commitment spine (SP-01) still has something owed on this exact
    counterparty and subject, so the thread is live work whatever the mail looks
    like. DECIDED BY: the commitment's `counterparty` matching the sender —
    display name or address local part, folded to letters and digits — AND a
    STRICT MAJORITY of the commitment's topic tokens (length >= 4) appearing in
    the subject. The doctrine says "naming this counterparty+topic"; measured on
    run148, counterparty alone matches 9 threads and the pair matches 1.

    FAILURE MODE: both halves are string matches, so a counterparty whose spine
    name and mail address share no letters (a nickname, an assistant sending on
    their behalf) is missed.
    """
    if not open_commitments:
        return False
    who = _identity_tokens(sender)
    if not who:
        return False
    subj = set(_words(subject))
    for c in open_commitments:
        if _identity_tokens(c.get("counterparty")) != who:
            continue
        topic = [t for t in _words(c.get("topic")) if len(t) >= _TOPIC_MIN_TOKEN]
        if not topic:
            continue
        if sum(1 for t in topic if t in subj) * 2 > len(topic):
            return True
    return False


def _identity_tokens(who: Any) -> str:
    """A person reduced to letters+digits: `Ana Exemplo` and
    `ana.exemplo@example.com` both become `anaexemplo`."""
    s = fold(who)
    if "@" in s:
        s = s.split("@", 1)[0]
    return re.sub(r"[^a-z0-9]", "", s)


def _words(s: Any) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", fold(s)) if w]


def screens_ran_unresolved(*, body_opened: bool) -> bool:
    """The deterministic screens could not settle this row, because the
    content-level ones had no input.

    MEANING: `Held · uncertain` — the last screen in the documented order, the
    one that means "we looked and could not tell". DECIDED BY: a night opens at
    most `cap` bodies out of a full inbox enumeration, so on every row it did
    not open, the ask and deadline screens ran against nothing. A row whose body
    WAS read had every screen's input; one read and unusable is
    `body_unreadable`, which sits ABOVE this in the screen order.
    """
    return not body_opened


def run_date(run_id: str, *, fallback: _dt.date | None = None) -> _dt.date:
    """The date a run belongs to, off its own id (`2026-08-16-run148`). A
    deadline is live AS OF THE NIGHT: keying on the wall clock would make the
    same stored run produce different facts on different days."""
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", str(run_id or ""))
    if m:
        d = _safe_date(*(int(x) for x in m.groups()))
        if d:
            return d
    return fallback or _dt.date.today()


def signals_for_row(row: dict[str, Any], corpus_row: dict[str, Any], *,
                    now: _dt.date,
                    open_commitments: list[dict[str, Any]]) -> dict[str, Any]:
    """The five facts for one thread, plus the leg each content fact came from.

    `row` is the driver ledger row; `corpus_row` is the CAP-01 capture-corpus
    row for the same `conversation_id`. Nothing here reads a model answer.
    """
    prov = (corpus_row.get("provenance") or {}) if corpus_row else {}
    subject = prov.get("subject")
    text = (corpus_row or {}).get("text") or ""
    opened = bool(row.get("body_opened"))
    # The extraction RESULT lives on the corpus row (the ledger records the
    # verdict, the corpus records what the read actually returned).
    extraction = (corpus_row or {}).get("extraction")
    unreadable = body_unreadable(
        body_opened=opened, body_chars=int(row.get("body_chars") or 0),
        text=text, subject=subject,
        extraction_error=(extraction or {}).get("error")
        if isinstance(extraction, dict) else None)
    # An unreadable body contributes NO content evidence — reading markers out
    # of a UI placeholder would be reading the mail client, not the mail.
    new_text = "" if unreadable else newest_message_text(text)
    ask, ask_leg = unanswered_direct_ask(subject=subject, new_text=new_text)
    dl, dl_leg = live_deadline(subject=subject, new_text=new_text, now=now)
    return {
        "unanswered_direct_ask": ask,
        "unanswered_direct_ask_leg": ask_leg,
        "live_deadline": dl,
        "live_deadline_leg": dl_leg,
        "open_spine_commitment": open_spine_commitment(
            sender=prov.get("sender"), subject=subject,
            open_commitments=open_commitments),
        "body_unreadable": unreadable,
        "screens_ran_unresolved": screens_ran_unresolved(body_opened=opened),
    }


def open_commitments(vault) -> list[dict[str, Any]]:
    """Every OPEN row of the commitment spine, or `[]` when there is none.
    Host-only by construction: `brain.spine` reads
    `<vault>/.brain/cos/host/commitments.sqlite`, off every VM mount."""
    try:
        from brain import spine                                  # noqa: PLC0415
        return [c for c in spine.list_all(vault) if c.get("status") == "open"]
    except Exception:                                    # pragma: no cover
        # No spine database on this host is not a reason to fail a night: the
        # fact is simply False, and it is False for a stated reason.
        return []
