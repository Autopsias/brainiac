"""Give a too-long note a short id before the synthesis sign-drain signs it.

2026-10-25: the confined session named 20 of its 45 notes after a whole
sentence (up to 208 characters). `notes.safe_slug` refuses an id over
MAX_SLUG_BYTES, so all 20 stayed unsigned and the run failed rc=75.

Reads the drain's file list (one path per line), and for each note whose
file stem is over the limit AND equals its frontmatter `id:`, moves it to
`<first words>-<8 hex of the old id>.md` and rewrites that one `id:` line.
Anything else is left exactly as it was, so the drain still refuses it.
Then rewrites `[[old-id]]` links to `[[new-id]]` in every listed note: the
same session wrote the notes that link to these, and on 2026-10-26 a rename
without this left 18 links pointing at files that no longer existed.
Prints the list back with the new paths. Stdlib only: the script runs it
with the host's `python3`, outside the engine's venv.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

#: Same number as `brain.notes.MAX_SLUG_BYTES` (a test holds them equal).
MAX_SLUG_BYTES = 180
#: Short enough to read, long enough to keep the subject.
KEEP_BYTES = 80


def short_id(old: str) -> str:
    head = old.encode("utf-8")[:KEEP_BYTES].decode("utf-8", "ignore")
    if "-" in head:
        head = head.rsplit("-", 1)[0]
    return f"{head.rstrip('-.')}-{hashlib.sha256(old.encode('utf-8')).hexdigest()[:8]}"


def shorten(path: Path) -> Path:
    old = path.stem
    if len(old.encode("utf-8")) <= MAX_SLUG_BYTES:
        return path
    lines = path.read_text(encoding="utf-8").split("\n")
    if not lines or lines[0].strip() != "---":
        return path
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return path                      # no `id:` line: leave it to the drain
        if line.startswith("id:"):
            if line[3:].strip().strip("\"'") != old:
                return path                  # the id is not the file name: leave it
            new = short_id(old)
            target = path.with_name(f"{new}.md")
            if target.exists():
                return path
            lines[i] = f"id: {new}"
            target.write_text("\n".join(lines), encoding="utf-8")
            path.unlink()
            return target
    return path


def relink(paths: list[str], renames: dict[str, str]) -> None:
    """Point `[[old]]`, `[[old|..]]` and `[[old#..]]` at the new id."""
    if not renames:
        return
    pattern = re.compile(r"\[\[(" + "|".join(map(re.escape, renames)) + r")(?=[\]|#])")
    for raw in paths:
        path = Path(raw)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new = pattern.sub(lambda m: "[[" + renames[m.group(1)], text)
        if new != text:
            path.write_text(new, encoding="utf-8")


def main(argv: list[str]) -> int:
    # "\n" only, as the shell's `read` splits: splitlines() also breaks on
    # \r, \x1c, \u2028 ..., so a crafted file name could add a list entry.
    with open(argv[1], encoding="utf-8", newline="") as fh:  # no \r translation
        entries = fh.read().split("\n")
    out: list[str] = []
    renames: dict[str, str] = {}
    for raw in entries:
        if raw:
            try:
                new = shorten(Path(raw))
            except (OSError, UnicodeDecodeError):
                new = Path(raw)
            if str(new) != raw:
                renames[Path(raw).stem] = new.stem
            out.append(str(new))
    relink(out, renames)
    for line in out:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
