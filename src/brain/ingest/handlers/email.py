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
from pathlib import Path
from typing import Optional

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
    _SKIP = {"script", "style", "head", "meta", "link"}

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


class EmailHandler(Handler):
    extensions = (".eml",)
    dependency_name = "stdlib"

    @classmethod
    def available(cls) -> bool:
        return True

    @classmethod
    def extract(cls, path: Path) -> ExtractResult:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > MAX_EML_BYTES:
            return ExtractResult.quarantine(
                "file_too_large", warnings=[f"{size} bytes exceeds cap {MAX_EML_BYTES}"]
            )
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return ExtractResult.quarantine("eml_read_error", warnings=[f"{type(exc).__name__}: {exc}"])
        try:
            msg = email.message_from_bytes(raw, policy=email.policy.default)
        except Exception as exc:
            return ExtractResult.quarantine("eml_parse_error", warnings=[f"{type(exc).__name__}: {exc}"])

        subject = strip_control_chars(_decode_header(msg.get("Subject")))
        from_addrs = _addr_list(_decode_header(msg.get("From")))
        to_addrs = _addr_list(_decode_header(msg.get("To")))
        cc_addrs = _addr_list(_decode_header(msg.get("Cc")))
        sent_raw = _decode_header(msg.get("Date"))
        sent_iso = _sent_date_iso(sent_raw)
        body_text, warnings = _extract_body(msg)

        try:
            attachments = list(msg.iter_attachments())
        except Exception:
            attachments = []
        if len(attachments) > MAX_ATTACHMENTS:
            warnings.append(f"attachments_truncated: {len(attachments)} found, cap {MAX_ATTACHMENTS}")
            attachments = attachments[:MAX_ATTACHMENTS]

        nested: list[dict] = []
        attach_meta: list[tuple[str, str, int]] = []
        total_bytes = 0
        for idx, part in enumerate(attachments, start=1):
            name = strip_control_chars(part.get_filename() or f"attachment_{idx}.bin")
            try:
                data = part.get_payload(decode=True) or b""
            except Exception as exc:
                warnings.append(f"attachment_decode_failed:{name}:{type(exc).__name__}")
                continue
            if total_bytes + len(data) > MAX_ATTACHMENT_TOTAL_BYTES:
                warnings.append(f"attachment_byte_cap_reached: stopped before {name}")
                break
            total_bytes += len(data)
            attach_meta.append((name, part.get_content_type(), len(data)))
            nested.append({"name": name, "data": data})

        from_disp = "; ".join(from_addrs) if from_addrs else _EM_DASH
        to_disp = "; ".join(to_addrs) if to_addrs else _EM_DASH

        lines = [
            "## Email metadata", "",
            f"- **Subject:** {subject or '(no subject)'}",
            f"- **From:** {from_disp}",
            f"- **To:** {to_disp}",
        ]
        if cc_addrs:
            lines.append(f"- **Cc:** {'; '.join(cc_addrs)}")
        if sent_iso:
            lines.append(f"- **Sent:** {sent_iso} (raw: {sent_raw})")
        elif sent_raw:
            lines.append(f"- **Sent:** {sent_raw}")
        if attach_meta:
            lines.append(f"- **Attachments:** {len(attach_meta)}")
        lines += ["", "## Body", "", body_text or "*(empty body)*", ""]
        if attach_meta:
            lines.append("## Attachments")
            lines.append("")
            for name, ctype, nbytes in attach_meta:
                lines.append(f"- `{name}` — {ctype} ({nbytes / 1024:.1f} KB)")
            lines.append("")
        body_md = "\n".join(lines)

        reason = density_gate(body_md)
        if reason:
            return ExtractResult.quarantine(reason, warnings=warnings)
        return ExtractResult(
            markdown=body_md, warnings=warnings,
            metadata={"nested": nested, "attachment_count": len(attach_meta), "subject": subject},
        )
