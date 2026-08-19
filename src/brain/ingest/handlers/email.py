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
    except Exception:
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
    except Exception:
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


def _extract_body(msg: "email.message.Message") -> tuple[str, list[str]]:
    warnings: list[str] = []
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
            return text_part.get_content().strip(), warnings
        except Exception:
            payload = text_part.get_payload(decode=True) or b""
            charset = text_part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace").strip(), warnings

    if html_part is not None:
        try:
            raw_html = html_part.get_content()
        except Exception:
            payload = html_part.get_payload(decode=True) or b""
            charset = html_part.get_content_charset() or "utf-8"
            raw_html = payload.decode(charset, errors="replace")
        warnings.append("html_only_fallback: no text/plain part, stripped HTML")
        return _strip_html(raw_html), warnings

    warnings.append("no_body_part: neither text/plain nor text/html present")
    return "", warnings


def _read_message(path: Path) -> Message | ExtractResult:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size > MAX_EML_BYTES:
        return ExtractResult.quarantine(
            "file_too_large",
            warnings=[f"{size} bytes exceeds cap {MAX_EML_BYTES}"],
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return ExtractResult.quarantine(
            "eml_read_error",
            warnings=[f"{type(exc).__name__}: {exc}"],
        )
    try:
        return email.message_from_bytes(raw, policy=email.policy.default)
    except Exception as exc:
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
    except Exception:
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
    except Exception as exc:
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
) -> str:
    lines = [
        "## Email metadata",
        "",
        f"- **Subject:** {subject or '(no subject)'}",
        f"- **From:** {'; '.join(from_addrs) if from_addrs else _EM_DASH}",
        f"- **To:** {'; '.join(to_addrs) if to_addrs else _EM_DASH}",
    ]
    if cc_addrs:
        lines.append(f"- **Cc:** {'; '.join(cc_addrs)}")
    if sent_iso:
        lines.append(f"- **Sent:** {sent_iso} (raw: {sent_raw})")
    elif sent_raw:
        lines.append(f"- **Sent:** {sent_raw}")
    if attachments:
        lines.append(f"- **Attachments:** {len(attachments)}")
    lines += ["", "## Body", "", body_text or "*(empty body)*", ""]
    if attachments:
        lines += ["## Attachments", ""]
        lines.extend(
            f"- `{name}` — {content_type} ({size / 1024:.1f} KB)"
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
        body_text, warnings = _extract_body(msg)
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
        )
        reason = density_gate(body_md)
        if reason:
            return ExtractResult.quarantine(reason, warnings=warnings)
        provenance = _email_provenance(msg, subject, from_addrs, sent_iso, sent_raw)
        return ExtractResult(
            markdown=body_md, warnings=warnings,
            metadata={"nested": nested, "attachment_count": len(attach_meta),
                      "subject": subject,
                      "provenance": provenance},
        )
