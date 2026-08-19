"""The ledger-side scan of `cos_contract` — run-scoped ledger rows, guard-stop corroboration (batch-2 drain).

The run-token spellings, `run_scoped_rows`, the chip-clear counter, the
guard-stop vocabulary/corroboration and the owner's lane pin moved verbatim out
of `cos_contract` and are re-imported by it, so `cc._run_token`,
`cc.guard_stop_corroborated` and `cc.lane_pin` keep their module path.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_reconcile_metrics import _rows, counts_archive, counts_draft  # noqa: E402

TOOLSETS = ("iab", "chrome-plugin")

#: The CLOSED vocabulary of stops that may excuse a row from its disposition.
#: A run may not invent one: doctrine names the guard, and the guard's own
#: ledger reason word is what corroborates it (E30(c)).
GUARD_STOPS = ("target-identity-mismatch",)

#: Where a recorded guard stop leaves its own trace. The checker DERIVES the
#: corroboration from these — the `guard_stop` record alone is the run's word.
#:
#: THE ACTION LEDGER HAS NO v7 PRODUCER AND IS KEPT ANYWAY (s10, 2026-08-16).
#: `_cos_action_ledger_*` was written by the MODEL leg of the pre-v7 browser
#: lane; the v7 model legs run `--tools "Read,Glob"` with editing denied and
#: cannot write a file at all. It is NOT removed, for two measured reasons:
#:   1. `cos_runverify.check_contract` RE-EXECUTES this checker over every run
#:      it scores, including historical ones. Six runs in the reference vault
#:      declare a `target-identity-mismatch` stop, and for TWO of them
#:      (2026-08-09-run104, 2026-08-10-run112) the ingestion ledger carries
#:      ZERO corroborating rows and the action ledger carries all of them.
#:      Dropping this glob flips those two from PASS to
#:      `OC-guard-stop-uncorroborated` on the next re-verification — rewriting
#:      history from a change of reader, not a change of fact.
#:   2. Its input is not "permanently absent": nine action ledgers are on disk.
#: Direction check, because a dead reader is only safe when it fails CLOSED:
#: this glob can only ever ADD corroboration, so its silence on a v7 night
#: cannot buy a PASS — an uncorroborated stop is refused. The v7 lane declares
#: no guard stop at all (`cos_driver.build_contract_inputs` writes no
#: `guard_stop` key), so `guard_stop_corroborated` is not even reached; the
#: click-era identity risk it scores is the same one `cos_runverify` RETIRED
#: with `target_identity`, because the REST lane addresses a conversation by id.
GUARD_STOP_GLOBS = ("_cos_ingestion_ledger_*.jsonl", "_cos_action_ledger_*.jsonl")


def counts_chip_clear(rows: list[dict]) -> int:
    """Verified chip CLEARS (LIF-01 `action: cleared`), never adds/re-levels."""
    n = 0
    for r in rows:
        if r.get("action") != "cleared":
            continue
        ver = r.get("verification")
        if isinstance(ver, dict):
            n += 1
        elif isinstance(ver, str) and ver.startswith(
            ("verified", "response-confirmed", "server-reread-confirmed", "PASS")
        ):
            n += 1
        elif str(r.get("status") or "").startswith(("verified", "response-confirmed")):
            n += 1
    return n


#: Every spelling of a run id that the COS surfaces actually produce:
#: `108`, `run108`, and — since MAN-01 (v5.58) told the run to take its
#: identity from the host's manifest sheet verbatim — `2026-08-09-run108`.
_RUN_TOKEN_RE = re.compile(r"(?:^|-)run(\d+)$|^(\d+)$")


def _run_token(value: object) -> str:
    """The RUN NUMBER, from whichever spelling of the id was handed over.

    WHY THIS IS NOT `lstrip("run")` ANY MORE (measured, run 108, 2026-08-09).
    MAN-01 made the run read its identity off the host's manifest sheet, whose
    `run_id` is the FULL `<date>-run<N>`. Run 108 obeyed: it stamped
    `2026-08-09-run108` into `scan_provenance.run_id` and into every ledger
    row, and invoked this checker with `--run-id 2026-08-09-run108`, which
    passed. The HOST validator re-executes the same checker with
    `cos_runverify._run_number(run_id)` — the bare `108` — and the old token
    (a leading-`run` strip, nothing more) made those two spellings unequal.
    Two consequences on one night: `scan_provenance.run_id must match
    --run-id` raised Malformed, so a genuine PASS block scored `contract:
    FAIL`; and `run_scoped_rows` matched NONE of the run's 423 ledger rows,
    which is the `OC-a-unaccounted` shape arriving from a spelling difference
    rather than from missing work.

    The run NUMBER is what every other joiner in this system already scopes on
    (`_file_run`, `canonical_block_path`, `cos_runverify._run_number`), and a
    run that writes under a foreign DATE has its own check
    (`cos_runverify.check_artifact_naming`, built for exactly that run-64
    defect) — so collapsing to the number here adds no blind spot it covers.
    A value that is no recognised spelling is returned unchanged, so it can
    still only match itself.
    """
    text = str(value).strip()
    m = _RUN_TOKEN_RE.search(text)
    return (m.group(1) or m.group(2)) if m else text


def _file_run(path: Path) -> str | None:
    m = re.search(r"-run([^.]+)\.jsonl$", path.name)
    return m.group(1) if m else None


def run_scoped_rows(ledgers: Path, glob: str, run_id: str) -> tuple[list[dict], int]:
    """Rows attributable to `run_id`, plus the count of unattributable rows.

    A row is this run's when it carries `run`/`run_id` matching, or when it
    lives in a `…-run<N>.jsonl` file for this run. A row with NO attribution in
    a file with NO run token cannot be proven to be this run's, so it is
    SKIPPED (and surfaced) — v5.27 already requires per-run attribution.
    """
    want = _run_token(run_id)
    keep: list[dict] = []
    unattributed = 0
    for path in sorted(ledgers.glob(glob)):
        file_run = _file_run(path)
        for row in _rows(path):
            rid = row.get("run", row.get("run_id"))
            if rid is not None:
                if _run_token(rid) == want:
                    keep.append(row)
            elif file_run is not None:
                if _run_token(file_run) == want:
                    keep.append(row)
            else:
                unattributed += 1
    return keep, unattributed


# --- guard stops: a stop halts action, never accounting (v5.52) --------------

def _guard_stop_shape(post: dict, enumerated: set[str]) -> dict | None:
    """The declared `guard_stop`, or None when it is absent or unusable.

    Shape only — whether the stop actually HAPPENED is decided from the run's
    own ledgers by `guard_stop_corroborated`, never from this record.
    """
    record = post.get("guard_stop")
    if not isinstance(record, dict):
        return None
    if record.get("guard") not in GUARD_STOPS:
        return None
    convid = record.get("convid")
    if not isinstance(convid, str) or convid not in enumerated:
        return None
    return record


def guard_stop_corroborated(ledgers: Path, run_id: str, guard: str) -> bool:
    """Did THIS run's own ledgers record the named guard firing?

    The stop's evidence is the reason word doctrine already requires on the row
    the guard fired on — `held_reason` on the ingestion ledger (E30(c)) or
    `action` on the action ledger. A run that declares a stop it never ledgered
    is asserting the one thing that would excuse its unaccounted rows, which is
    exactly the shape this checker refuses everywhere else.
    """
    for glob in GUARD_STOP_GLOBS:
        rows, _ = run_scoped_rows(ledgers, glob, run_id)
        for row in rows:
            if guard in (row.get("held_reason"), row.get("action")):
                return True
    return False


# --- the elected-lane pin (owner overlay, v5.52) -----------------------------

def lane_pin(ledgers: Path) -> str | None:
    """The owner's pinned browser toolset, from `overlay/cos/browser-lane.md`.

    ABSENT file, absent key, or any unrecognised value ⇒ **no pin** and the
    ordinary IAB-first election stands. Owner configuration, so it is read from
    the vault beside the ops dir and never supplied by the run — a pin the run
    could declare for itself is a pin a silent fallback can drop.
    """
    path = ledgers.parent / "overlay" / "cos" / "browser-lane.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    body = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S)
    m = re.search(r"^pin:[ \t]*(\S+)[ \t]*$", body, re.M)
    if m is None or m.group(1) not in TOOLSETS:
        return None
    return m.group(1)
