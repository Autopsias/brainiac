"""``brain exceptions`` — find and open the page that says what needs you.

The page itself is written by ``exceptions_page.generate()`` at the end of
every ``brain maintain`` run. Until this command existed there was no way to
reach it except knowing the path by heart, which meant three harnesses
(Claude Code, Codex, Cowork) each needed the owner to remember a different
one. This is the one command all three call.

**One user has many vaults.** On the host it sweeps the workspace registry
and reports EVERY registered vault, the same way ``alerts`` does — a vault
whose page is missing is a reported row, never an omitted one. On the VM leg
there is one vault and no registry, so it reports that vault alone.

**Three ways out, because the harnesses differ.** ``--open`` hands the page
to the desktop (Claude Code on a Mac); ``--text`` prints it as plain text
(Codex in a terminal, Cowork in a Linux sandbox with no browser); the default
prints one line per vault with the count and the path.
"""

from __future__ import annotations

import html.parser
import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any

from . import config as _config
from . import exceptions_page as _page

#: Where the two pages live, relative to what ``generate()`` writes. The FULL
#: page is deliberately off the shared mount (it carries real note ids and
#: question text); the mount page is the tier-gated copy a VM may read.
_FULL = _page.FULL_FILENAME
_MOUNT = _page.MOUNT_FILENAME
_JSON = _page.JSON_FILENAME


class _TextPage(html.parser.HTMLParser):
    """The page as plain text.

    Deliberately a parser over the page's OWN markup rather than a general
    HTML-to-text converter: this module and ``exceptions_render`` ship
    together, so the tag vocabulary is known and small. It exists for the two
    harnesses that have no browser at all."""

    _BREAK_BEFORE = {"h1", "h2", "p", "li", "ul", "section", "header"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("style", "script"):
            self._skip += 1
        elif tag in self._BREAK_BEFORE:
            self._out.append("\n")
        elif tag in ("br",):
            self._out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._out.append(data)

    def text(self) -> str:
        raw = "".join(self._out)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        kept: list[str] = []
        for line in lines:
            if line or (kept and kept[-1]):
                kept.append(line)
        return "\n".join(kept).strip()


def page_to_text(html_text: str) -> str:
    """Render one exceptions page as plain text."""
    parser = _TextPage()
    parser.feed(html_text)
    return parser.text()


def _read_summary(vault: Path) -> dict[str, Any] | None:
    try:
        raw = (_config.brain_runtime_dir(vault) / _JSON).read_text(
            encoding="utf-8")
    except OSError:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _full_page_path(vault: Path) -> Path:
    return _config.proven_off_mount(
        _config.index_dir(vault), vault, what="exceptions full page") / _FULL


def vault_row(vault: Path, *, role: str) -> dict[str, Any]:
    """One vault's row: which page to read, how many things need the owner,
    and — when the answer is not knowable — WHY, never a cheerful zero.

    A missing summary is the same finding ``alerts`` reports: the engine that
    ran this vault's ``maintain`` predates the page, so the count is UNKNOWN.
    Reporting 0 there would be a fabricated all-clear."""
    name = vault.parent.name or str(vault)
    mount = _config.brain_runtime_dir(vault) / _MOUNT
    page = mount if role == "vm" else _full_page_path(vault)
    summary = _read_summary(vault)
    if summary is None:
        return {"vault": str(vault), "name": name, "page": str(page),
                "count": None, "exists": page.is_file(),
                "reason": "this vault's nightly run has not written a "
                          "summary yet"}
    count = summary.get("count")
    return {"vault": str(vault), "name": name, "page": str(page),
            "count": int(count) if isinstance(count, int) else None,
            "exists": page.is_file(),
            "reason": None if isinstance(count, int) else
            "the summary carries no count"}


def collect(*, role: str = "host", vault: Path | None = None,
            home: Path | None = None) -> dict[str, Any]:
    """Every vault this role can see, with its page. Mirrors
    ``alerts.collect``'s host-sweeps-the-registry / vm-sees-one shape so the
    two surfaces can never disagree about which vaults exist."""
    from . import alerts as _alerts

    if role == "vm":
        vaults = [vault] if vault else []
    else:
        vaults = list(_alerts.host_vaults(home or Path.home()))
        if not vaults and vault:
            vaults = [vault]
    return {"role": role,
            "vaults": [vault_row(Path(v), role=role) for v in vaults]}


def open_in_desktop(path: Path) -> str:
    """Hand one page to the desktop. Returns a plain sentence saying what
    happened — a sandbox with no browser is an ordinary outcome here, not an
    error, so it is reported rather than raised."""
    if not path.is_file():
        return f"nothing to open — {path} does not exist yet"
    system = platform.system()
    if system == "Darwin":
        cmd = ["open", str(path)]
    elif system == "Windows":  # pragma: no cover - not exercised on CI
        cmd = ["cmd", "/c", "start", "", str(path)]
    else:
        cmd = ["xdg-open", str(path)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return (f"could not open a browser here ({exc.__class__.__name__}). "
                f"The page is at {path}")
    return f"opened {path}"


def render_human(report: dict[str, Any]) -> str:
    """The default output: one line per vault, and the path on the next."""
    rows = report["vaults"]
    if not rows:
        return ("No vault found. Run `brain exceptions --vault <path>`, or "
                "install a vault with `brain init --full`.")
    out: list[str] = []
    for row in rows:
        if row["count"] is None:
            head = f"{row['name']}: unknown, because {row['reason']}"
        elif row["count"] == 0:
            head = f"{row['name']}: nothing needs you"
        else:
            n = row["count"]
            head = f"{row['name']}: {n} thing{'s' if n != 1 else ''} need" \
                   f"{'' if n != 1 else 's'} you"
        out.append(head)
        if row["exists"]:
            out.append(f"    {row['page']}")
        else:
            out.append("    (no page yet — it appears after the next "
                       "`brain maintain` run)")
    out.append("")
    out.append("Open it with `brain exceptions --open`, or read it here with "
               "`brain exceptions --text`.")
    return "\n".join(out)


def render_text(report: dict[str, Any]) -> str:
    """Every vault's page, as plain text. For a harness with no browser."""
    chunks: list[str] = []
    for row in report["vaults"]:
        path = Path(row["page"])
        chunks.append(f"===== {row['name']} =====")
        if not path.is_file():
            # No nested quoting inside the f-string: the VM leg is Python 3.10,
            # where same-quote nesting is a SyntaxError, not a style choice.
            why = row["reason"] or ("It appears after the next "
                                    "`brain maintain` run.")
            chunks.append(f"No page yet. {why}")
            continue
        try:
            chunks.append(page_to_text(path.read_text(encoding="utf-8")))
        except OSError as exc:
            chunks.append(f"Could not read {path}: {exc}")
    return "\n\n".join(chunks)


def open_all(report: dict[str, Any]) -> str:
    return "\n".join(open_in_desktop(Path(r["page"]))
                     for r in report["vaults"]) or "no vault to open"


def _demo() -> None:  # pragma: no cover - hand probe
    print(render_human(collect(role=os.environ.get("BRAIN_ROLE", "host"))))
