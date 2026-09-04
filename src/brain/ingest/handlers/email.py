"""Email (.eml, RFC 5322) handler — stdlib only (``email`` + ``html.parser``,
mirrors the sha256-verified reference-vault reference, ADR-0003 Appendix B).
Produces headers + body + an attachment manifest; each attachment's bytes are
returned via ``metadata["nested"]`` so the orchestrator (pipeline.py
``_process_nested``) re-enters the dispatcher for each one — bounded by
``MAX_ATTACHMENTS``/``MAX_ATTACHMENT_TOTAL_BYTES`` here, and by depth +
a shared byte/count budget at the pipeline layer (defense in depth against a
crafted attachment-of-attachment chain)."""
from __future__ import annotations

import email
import email.policy
import email.utils
import html.parser
import re
from email.message import Message
from pathlib import Path
from typing import Any, Optional

from . import concealment
from .base import ExtractResult, Handler, density_gate, strip_control_chars

MAX_EML_BYTES = 50 * 1024 * 1024
MAX_ATTACHMENTS = 50
MAX_ATTACHMENT_TOTAL_BYTES = 200 * 1024 * 1024  # matches pipeline.MAX_INGEST_BYTES

_EM_DASH = "—"


class _HtmlStripper(html.parser.HTMLParser):
    """Minimal stdlib HTML->text fallback for a text/html-only body (no
    BeautifulSoup dependency needed for this narrow use)."""

    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "blockquote", "pre", "hr"}
    # Only skip tags that ALWAYS emit a closing tag. A tag that can go unclosed
    # latches `_skip` at >0 and swallows the rest of the message:
    #   * VOID elements (meta, link, br, hr, img) never fire handle_endtag at all
    #     — `<meta charset>` mail used to extract to the empty string;
    #   * `head` has an OPTIONAL end tag, so `<head><meta><body>` swallowed it too.
    # This is the set handlers/html.py already proved out; keep the two in step.
    _SKIP = {"script", "style", "noscript", "svg", "canvas", "iframe", "object", "embed"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip > 0:
            self._skip -= 1
        elif tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._chunks.append(data)

    def get_text(self) -> str:
        joined = "".join(self._chunks)
        joined = re.sub(r"\n{3,}", "\n\n", joined)
        return "\n".join(line.rstrip() for line in joined.split("\n")).strip()


def _strip_html(raw: str) -> str:
    stripper = _HtmlStripper()
    try:
        stripper.feed(raw)
        stripper.close()
    except Exception:  # coverage-audit: returns no body text, so nothing is admitted to cover
        return ""
    return stripper.get_text()


def _decode_header(raw: object) -> str:
    return str(raw).strip() if raw else ""


def _addr_list(raw: str) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for name, addr in email.utils.getaddresses([raw]):
        name, addr = name.strip(), addr.strip()
        if name and addr:
            out.append(f"{name} <{addr}>")
        elif addr:
            out.append(addr)
        elif name:
            out.append(name)
    return out


def _sent_date_iso(raw: str) -> Optional[str]:
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        return dt.isoformat() if dt is not None else None
    except Exception:  # coverage-audit: a header value, admitted under email:headers either way
        return None


def _conversation_id(msg: "email.message.Message") -> Optional[str]:
    """The THREAD's identity, not this message's: the root of the References
    chain when there is one (every reply in a thread then agrees), else the
    In-Reply-To parent, else this message's own Message-ID (a thread of one)."""
    refs = _decode_header(msg.get("References"))
    if refs:
        first = refs.split()
        if first:
            return strip_control_chars(first[0])
    parent = _decode_header(msg.get("In-Reply-To")).split()
    if parent:
        return strip_control_chars(parent[0])
    mid = _decode_header(msg.get("Message-ID"))
    return strip_control_chars(mid) if mid else None


def _extract_body(
    msg: "email.message.Message", seen: Any = None,
) -> tuple[str, list[str], list[dict[str, Any]] | None, str]:
    """The body text, warnings, any CONCEALED runs, and the body's SOURCE name.

    The fourth element is what the coverage ledger records the body under, and
    it is what decides the note's scan state: ``email:body_html`` is walked and
    covered, ``email:body_plain`` is not, so a text/plain mail still reads
    ``concealment_scan: unknown`` rather than inheriting a ``full`` it never
    earned (V9, 2026-09-02) — now because the source has no walker in
    ``COVERED_SOURCES``, not because a boolean said so.

    The concealment walk runs on the html-only branch and ONLY there, because
    that is the only branch whose bytes reach the Markdown: when a
    ``text/plain`` part exists it is what gets extracted, and styling in the
    unused html alternative never reaches a model.

    Mail is this project's primary ingest lane, and until 2026-09-02 this
    branch was a one-line bypass of the whole handler-boundary control
    (review finding V1): identical white-on-white bytes gave ``conceal`` as
    ``.html`` and ``instruction_only`` — admitted, payload in the signed
    note — as an html-only ``.eml``. The attacker picks the MIME structure.
    """
    warnings: list[str] = []
    # `None` until a branch WALKS. A text/plain body never reaches the
    # concealment detector, so its source is uncovered and the note is
    # stamped `unknown` rather than `full` (V9, 2026-09-02).
    concealed: list[dict[str, Any]] | None = None
    text_part = html_part = None
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and text_part is None:
            text_part = part
        elif ctype == "text/html" and html_part is None:
            html_part = part

    if text_part is not None:
        try:
            return (text_part.get_content().strip(), warnings, concealed,
                    "email:body_plain")
        except Exception:  # coverage-audit: the same part, decoded by hand; email:body_plain is uncovered regardless
            payload = text_part.get_payload(decode=True) or b""
            charset = text_part.get_content_charset() or "utf-8"
            return (payload.decode(charset, errors="replace").strip(),
                    warnings, concealed, "email:body_plain")

    if html_part is not None:
        try:
            raw_html = html_part.get_content()
        except Exception:  # coverage-audit: the same bytes, decoded by hand, and the walk below reads that same string
            payload = html_part.get_payload(decode=True) or b""
            charset = html_part.get_content_charset() or "utf-8"
            raw_html = payload.decode(charset, errors="replace")
        warnings.append("html_only_fallback: no text/plain part, stripped HTML")
        # `_HtmlStripper` below sees text only. This is the last point that
        # can still see colour, size, position and display state (M-3).
        # The walk REPORTS what it read into the ledger's sink, and the
        # ledger holds that report against the stripped text admitted below —
        # two readings of the same bytes, compared, rather than a table
        # asserting they agree (V15, 2026-09-03).
        concealed = concealment.collect(
            lambda: concealment.html_runs_stdlib(raw_html, seen), warnings)
        return _strip_html(raw_html), warnings, concealed, "email:body_html"

    warnings.append("no_body_part: neither text/plain nor text/html present")
    return "", warnings, concealed, ""


def _read_message(path: Path) -> Message | ExtractResult:
    try:
        size = path.stat().st_size
    except OSError:  # coverage-audit: an unreadable size falls through to the gates below
        size = 0
    if size > MAX_EML_BYTES:
        return ExtractResult.quarantine(
            "file_too_large",
            warnings=[f"{size} bytes exceeds cap {MAX_EML_BYTES}"],
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:  # coverage-audit: quarantines; no text is admitted, so none is claimed
        return ExtractResult.quarantine(
            "eml_read_error",
            warnings=[f"{type(exc).__name__}: {exc}"],
        )
    try:
        return email.message_from_bytes(raw, policy=email.policy.default)
    except Exception as exc:  # coverage-audit: quarantines; no text is admitted, so none is claimed
        return ExtractResult.quarantine(
            "eml_parse_error",
            warnings=[f"{type(exc).__name__}: {exc}"],
        )


def _attachment_payloads(
    msg: Message,
    warnings: list[str],
) -> tuple[list[dict[str, Any]], list[tuple[str, str, int]]]:
    try:
        attachments = list(msg.iter_attachments())
    except Exception:  # coverage-audit: no manifest lines are admitted, so none are claimed
        attachments = []
    if len(attachments) > MAX_ATTACHMENTS:
        warnings.append(
            f"attachments_truncated: {len(attachments)} found, cap {MAX_ATTACHMENTS}"
        )
        attachments = attachments[:MAX_ATTACHMENTS]
    nested: list[dict[str, Any]] = []
    metadata: list[tuple[str, str, int]] = []
    total_bytes = 0
    for index, part in enumerate(attachments, start=1):
        name = strip_control_chars(part.get_filename() or f"attachment_{index}.bin")
        data = _decode_attachment(part, name, warnings)
        if data is None:
            continue
        if total_bytes + len(data) > MAX_ATTACHMENT_TOTAL_BYTES:
            warnings.append(f"attachment_byte_cap_reached: stopped before {name}")
            break
        total_bytes += len(data)
        metadata.append((name, part.get_content_type(), len(data)))
        nested.append({"name": name, "data": data})
    return nested, metadata


def _decode_attachment(part: Message, name: str, warnings: list[str]) -> bytes | None:
    try:
        return part.get_payload(decode=True) or b""
    except Exception as exc:  # coverage-audit: warns; the manifest line is admitted and checked like any other
        warnings.append(f"attachment_decode_failed:{name}:{type(exc).__name__}")
        return None


def _render_email(
    *,
    subject: str,
    from_addrs: list[str],
    to_addrs: list[str],
    cc_addrs: list[str],
    sent_raw: str,
    sent_iso: str | None,
    body_text: str,
    attachments: list[tuple[str, str, int]],
    admitted: Any,
    body_source: str,
) -> str:
    """Render the note, recording every chunk on the coverage ledger.

    A header line mixes the handler's own label with an attacker-supplied
    value, so the WHOLE line is admitted under ``email:headers`` rather than
    split — over-attributing a bold label to the document is harmless, and
    losing the value would not be.
    """
    head = "email:headers"
    lines = [
        admitted.chrome("## Email metadata"),
        "",
        admitted.add(head, f"- **Subject:** {subject or '(no subject)'}"),
        admitted.add(head,
                     f"- **From:** {'; '.join(from_addrs) if from_addrs else _EM_DASH}"),
        admitted.add(head,
                     f"- **To:** {'; '.join(to_addrs) if to_addrs else _EM_DASH}"),
    ]
    if cc_addrs:
        lines.append(admitted.add(head, f"- **Cc:** {'; '.join(cc_addrs)}"))
    if sent_iso:
        lines.append(admitted.add(head, f"- **Sent:** {sent_iso} (raw: {sent_raw})"))
    elif sent_raw:
        lines.append(admitted.add(head, f"- **Sent:** {sent_raw}"))
    if attachments:
        lines.append(admitted.chrome(f"- **Attachments:** {len(attachments)}"))
    lines += ["", admitted.chrome("## Body"), ""]
    lines += [admitted.add(body_source, body_text) if body_text
              else admitted.chrome("*(empty body)*"), ""]
    if attachments:
        lines += [admitted.chrome("## Attachments"), ""]
        lines.extend(
            admitted.add("email:attachment_manifest",
                         f"- `{name}` — {content_type} ({size / 1024:.1f} KB)")
            for name, content_type, size in attachments
        )
        lines.append("")
    return "\n".join(lines)


def _email_provenance(
    msg: Message,
    subject: str,
    from_addrs: list[str],
    sent_iso: str | None,
    sent_raw: str,
) -> dict[str, str]:
    values: dict[str, str | None] = {
        "sender": from_addrs[0] if from_addrs else None,
        "sent": sent_iso or sent_raw or None,
        "conversation_id": _conversation_id(msg),
        "subject": subject or None,
    }
    return {key: value for key, value in values.items() if value is not None}


class EmailHandler(Handler):
    extensions = (".eml",)
    dependency_name = "stdlib"

    @classmethod
    def available(cls) -> bool:
        return True

    @classmethod
    def extract(cls, path: Path) -> ExtractResult:
        msg = _read_message(path)
        if isinstance(msg, ExtractResult):
            return msg
        subject = strip_control_chars(_decode_header(msg.get("Subject")))
        from_addrs = _addr_list(_decode_header(msg.get("From")))
        to_addrs = _addr_list(_decode_header(msg.get("To")))
        cc_addrs = _addr_list(_decode_header(msg.get("Cc")))
        sent_raw = _decode_header(msg.get("Date"))
        sent_iso = _sent_date_iso(sent_raw)
        admitted = concealment.Admitted()
        body_text, warnings, concealed, body_source = _extract_body(
            msg, admitted.watch("email:body_html"))
        nested, attach_meta = _attachment_payloads(msg, warnings)
        body_md = _render_email(
            subject=subject,
            from_addrs=from_addrs,
            to_addrs=to_addrs,
            cc_addrs=cc_addrs,
            sent_raw=sent_raw,
            sent_iso=sent_iso,
            body_text=body_text,
            attachments=attach_meta,
            admitted=admitted,
            body_source=body_source,
        )
        reason = density_gate(body_md)
        if reason:
            return ExtractResult.quarantine(reason, warnings=warnings)
        provenance = _email_provenance(msg, subject, from_addrs, sent_iso, sent_raw)
        return ExtractResult(
            markdown=body_md, warnings=warnings,
            metadata={"nested": nested, "attachment_count": len(attach_meta),
                      "subject": subject, "concealed": concealed or [],
                      "provenance": provenance,
                      **concealment.attest(warnings, body=body_md,
                                          admitted=admitted)},
        )
