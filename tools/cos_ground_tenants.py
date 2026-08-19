"""D7b sender-classification inputs of `cos_ground` — the domain extractor, the tenant-domains overlay reader (batch-2 drain).

Moved verbatim out of `cos_ground`; every name is re-imported by the parent so
`cos_ground.extract_domain`, `classify_sender`, `load_tenant_domains` and
`GroundingRefused` keep their original module path and identity.
"""
from __future__ import annotations

import sys
import unicodedata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cos_ground_domain                                        # noqa: E402


class GroundingRefused(Exception):
    """A pre-flight condition that makes the whole fetch dishonest to attempt."""


# ---------------------------------------------------------------------------
# D7b · the domain extractor — eight rules, every refusal yielding `external`
# ---------------------------------------------------------------------------
def extract_domain(sender: str | None) -> str | None:
    """The domain part of a `From` string, or `None` — which classes external.

    Deliberately narrow. `cos_driver_page.js` computes `sender` as
    `From.Mailbox.EmailAddress || From.Mailbox.Name`, so the value may be a
    DISPLAY NAME with no `@` at all; and the string is attacker-chosen either
    way. Refusing to parse ~0 real rows is cheaper than parsing them wrongly,
    and the cheap direction (external) is the one that spends least vault.
    """
    if not sender:
        return None
    s = str(sender).strip()
    if "@" not in s:                                    # 1. display-name case
        return None
    if "<" in s and ">" in s:                           # 2. angle-address wins
        _head, _, tail = s.rpartition("<")
        s = tail.split(">", 1)[0].strip() if ">" in tail else ""
    if '"' in s:                                        # 3. quoted local part
        return None
    if s.count("@") != 1:                               # 4. exactly one `@`…
        return None
    local, _, domain = s.partition("@")
    if not local.strip():                               # …and a non-empty local
        return None
    domain = domain.strip()
    if domain.endswith("."):
        domain = domain[:-1]
    domain = unicodedata.normalize("NFC", domain).casefold()   # 5. NFC + casefold
    if not domain:
        return None
    # 6. Literal ASCII labels only — the whole label-shape cluster lives in
    #    `cos_ground_domain.literal_ascii_labels` (rule 6 of the eight, moved
    #    with its own comment): it closes the punycode/homograph hole in BOTH
    #    directions, refusing a Unicode homograph outright and comparing an
    #    `xn--…` label LITERALLY, so it can only ever equal a tenant domain an
    #    owner wrote in that same literal form.
    if not cos_ground_domain.literal_ascii_labels(domain):
        return None
    return domain
    # 7. The caller compares by EXACT string equality against the normalized
    #    overlay entries — never a suffix match, which would make
    #    `evil-example.com` internal. Subdomains are never implied.
    # 8. Plus-addressing lives in the LOCAL part and so cannot affect the
    #    domain. Recorded so nobody later "fixes" a non-bug on the wrong side.


def extract_address(sender: str | None) -> str | None:
    """The bare `local@domain` out of a `From` string, or `None`.

    D1's L1 query is the SENDER ADDRESS, not the display string: the exact
    alias/title leg answers when the owner has put the address in a person
    note's `aliases:`, and `Alice <alice@example.com>` is not that alias. Same
    parse as `extract_domain`, so the two can never disagree about which
    strings are addresses at all.
    """
    if not sender:
        return None
    s = str(sender).strip()
    if "@" not in s:
        return None
    if "<" in s and ">" in s:
        _head, _, tail = s.rpartition("<")
        s = tail.split(">", 1)[0].strip() if ">" in tail else ""
    domain = extract_domain(s)
    if not domain:
        return None
    local = s.partition("@")[0].strip()
    return f"{local}@{domain}" if local else None


def normalize_tenant_entry(raw: str) -> str | None:
    """One overlay tenant-domain list entry, normalized — or `None` to drop it.

    A malformed entry is a WARNING and is dropped, matching `ingest.md`'s
    documented fail-closed posture: an unparseable rule never infers the
    permissive answer. A leading `.` is rejected outright — this is an exact
    domain list, not a suffix matcher.
    """
    e = str(raw or "").strip()
    if e.startswith("@"):
        e = e[1:]
    e = unicodedata.normalize("NFC", e).casefold().strip()
    if not e or e.startswith("."):
        return None
    return extract_domain("x@" + e)


def list_lines(body: str) -> list[str]:
    """`body`'s lines, minus the two places a `- ` line is DOCUMENTATION.

    WHY THIS EXISTS (measured on the SHIPPED template, 2026-08-16). The reader
    scanned every `- ` line in the body, and the starter template
    (`overlay/template/cos/tenant-domains.md`) ends with its worked example
    inside an HTML comment. An UNTOUCHED copy therefore yielded
    `['example.com', 'example.co.uk']` — so `domains` was non-empty, the
    fail-closed `ungrounded-by-construction` branch never fired, and
    `--preflight` reported `senders-classifiable` on a fresh install that had
    declared no tenant domain at all. A commented-out example is the one thing
    in a template GUARANTEED not to be the owner's own answer.

    A fenced block is skipped for the same reason and it matters just as much:
    the template's `One list line per domain:` example sits in one.

    Only line-leading `<!--` opens a comment. That is the shape a template
    writes, and a parser that hunts the marker mid-line starts guessing about
    quoted text — this list is small enough that "the documentation is set off
    on its own lines" is the whole rule.
    """
    out: list[str] = []
    fence = ""
    in_comment = False
    for raw in body.splitlines():
        line = raw.strip()
        if in_comment:
            in_comment = "-->" not in line
            continue
        if fence:
            if line.startswith(fence):
                fence = ""
            continue
        if line.startswith("```") or line.startswith("~~~"):
            fence = line[:3]
            continue
        if line.startswith("<!--"):
            in_comment = "-->" not in line
            continue
        out.append(line)
    return out


def load_tenant_domains(vault: Path) -> tuple[list[str], list[str]]:
    """`(domains, warnings)` from the overlay `cos/` file whose frontmatter
    declares `setting: tenant-domains` (D7a).

    ABSENT is not an empty list — the caller must tell them apart, because with
    the key absent every sender classes external and grounding would be a shadow
    of itself while still calling itself grounded. Absent raises.
    """
    from brain import frontmatter                                # noqa: PLC0415
    from brain import overlay as ov                              # noqa: PLC0415

    cos_dir = ov.overlay_dir(vault) / "cos"
    warnings: list[str] = []
    if not cos_dir.is_dir():
        raise GroundingRefused("tenant-domains overlay missing: sender classes "
                               "cannot be computed")
    for f in sorted(cos_dir.glob("*.md")):
        try:
            meta, body = frontmatter.parse_text(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str((meta or {}).get("setting") or "") != "tenant-domains":
            continue
        domains: list[str] = []
        for line in list_lines(body):
            if not line.startswith("- "):
                continue
            entry = line[2:].split("#", 1)[0].strip()
            if not entry:
                continue
            norm = normalize_tenant_entry(entry)
            if norm is None:
                warnings.append(f"dropped malformed tenant-domain entry {entry!r}")
            elif norm not in domains:
                domains.append(norm)
        return domains, warnings
    raise GroundingRefused("tenant-domains overlay missing: sender classes "
                           "cannot be computed")


def classify_sender(domain: str | None, *, tenant_domains: list[str],
                    sender_note: dict[str, Any] | None, tracked_domain: bool
                    ) -> str:
    """`internal` | `counterparty` | `external` (D7).

    Exact string equality against the normalized overlay entries. Never a suffix
    match: a suffix matcher makes `evil-example.com` internal.
    """
    if domain and domain in tenant_domains:
        return "internal"
    if sender_note or tracked_domain:
        return "counterparty"
    return "external"
