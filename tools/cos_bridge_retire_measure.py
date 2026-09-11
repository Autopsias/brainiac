#!/usr/bin/env python3
"""DD-02 — the two things the rehearsal can only learn by DOING them.

Kept out of ``tools/cos_bridge_retire_duplicates.py`` because everything here
writes notes, and that module's dry run must be incapable of it. It is imported
lazily, only by the two measurement modes.

* :func:`measure_fixture` — how long ONE ``core.supersede`` takes end to end,
  on a throwaway vault sized to the target. Sizing matters more than it looks:
  ``supersede`` finishes with a full incremental ``sync`` that reads every
  markdown file in the vault, so a 200-note toy fixture measures ~0.37s where
  the real shape measures ~1.9s. At 1360 pairs that is the difference between
  "eight minutes" and "most of an hour" in the owner's decision.
* :func:`roundtrip_fixture` — apply, then undo, then compare the frontmatter
  byte for byte, so the receipt SHOWS the rollback works instead of asserting it.
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

_CONV_A = "AAQkADMyNTM0MDJj-A="
_CONV_B = "AAQkADMyNTM0MDJj-B="

#: (id, conversation_id, captured-text sha) — the shapes that matter: a group
#: of three spanning two days (with run9 BEFORE run10), a lone note with no
#: retirees, and a second conversation.
BRIDGE_FIXTURE = (
    ("cosbridge-2026-09-01-run9-aaaa", _CONV_A, "text-one"),
    ("cosbridge-2026-09-01-run10-aaaa", _CONV_A, "text-one"),
    ("cosbridge-2026-09-02-run2-aaaa", _CONV_A, "text-one"),
    ("cosbridge-2026-09-02-run11-bbbb", _CONV_A, "text-two"),
    ("cosbridge-2026-09-01-run3-cccc", _CONV_B, "text-one"),
    ("cosbridge-2026-09-01-run4-cccc", _CONV_B, "text-one"),
)


def bridge_note(nid: str, *, cid: str | None, sha: str | None) -> str:
    """A bridge note's frontmatter shape, trimmed to what this cleanup reads."""
    lines = ["---", f"id: {nid}", f'title: "{nid}"', "type: note",
             "classification: Internal", "created: 2026-01-01",
             "updated: 2026-01-01", "status: draft", "provenance.trust: untrusted"]
    if sha is not None:
        lines.append(f"cos.source_sha256: {sha}")
    if cid is not None:
        lines.append(f"provenance.conversation_id: {cid}")
    return "\n".join([*lines, "---", "", f"body of {nid}", ""])


def write_bridge_fixture(vault: Path, spec=BRIDGE_FIXTURE) -> Path:
    (vault / "brain" / "resources").mkdir(parents=True, exist_ok=True)
    (vault / "brain" / "index.md").write_text(
        "---\nid: index\ntitle: Index\ntype: index\nclassification: Internal\n"
        "created: 2026-01-01\nupdated: 2026-01-01\n---\n\nMap.\n", encoding="utf-8")
    for nid, cid, sha in spec:
        (vault / "brain" / "resources" / f"{nid}.md").write_text(
            bridge_note(nid, cid=cid, sha=sha), encoding="utf-8")
    return vault


def _throwaway(prefix: str) -> Path:
    """A temp vault root with a fresh signing key and its own index/runtime dirs.

    ``.resolve()`` is load-bearing on macOS: ``/var`` is a symlink to
    ``/private/var`` and ``supersede`` does ``path.relative_to(self.vault)``,
    which raises on the unresolved form.
    """
    from brain.audit import generate_key_pem

    priv, _pub = generate_key_pem()
    os.environ["BRAIN_AUDIT_KEY_PEM"] = priv.decode("utf-8")
    tmp = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    os.environ["BRAIN_RUNTIME_DIR"] = str(tmp / "rt")
    os.environ["BRAIN_INDEX_DIR"] = str(tmp / "idx")
    return tmp


def _core_over(tmp: Path, vault: Path):
    from brain.core import BrainCore
    from brain.embed import HashEmbedder
    from brain.index import BrainIndex
    from brain.vectors import BruteForceBackend

    index = BrainIndex(db_path=tmp / "index.sqlite", backend=BruteForceBackend(),
                       embedder=HashEmbedder())
    index.rebuild(vault)
    return BrainCore(vault=vault, index=index, audit_log=tmp / "audit.jsonl",
                     role="host")


# ------------------------------------------------------------ the cost ------

def _fixture_note(nid: str, body_bytes: int) -> str:
    head = (f"---\nid: {nid}\ntitle: \"{nid}\"\ntype: note\n"
            f"classification: Internal\ncreated: 2026-01-01\n"
            f"updated: 2026-01-01\n---\n\n")
    return head + ("lorem ipsum dolor sit amet. " * (max(body_bytes, 0) // 27 + 1))


def measure_fixture(notes: int, note_bytes: int, pairs: int) -> dict:
    """Time ``supersede`` END TO END on a throwaway vault of ``notes`` files."""
    tmp = _throwaway("retire-measure-")
    vault = tmp / "vault"
    (vault / "brain" / "resources").mkdir(parents=True)
    for i in range(notes):
        (vault / "brain" / "resources" / f"n{i}.md").write_text(
            _fixture_note(f"n{i}", note_bytes), encoding="utf-8")
    print(f"  fixture: {notes} notes written", flush=True)

    load_before = os.getloadavg()
    t0 = time.perf_counter()
    core = _core_over(tmp, vault)
    rebuild_s = time.perf_counter() - t0
    print(f"  fixture: indexed in {rebuild_s:.1f}s", flush=True)

    seconds = []
    for k in range(pairs):
        t = time.perf_counter()
        core.supersede(f"n{2 * k}", f"n{2 * k + 1}", reason="DD-02 cost measurement")
        seconds.append(round(time.perf_counter() - t, 4))
        print(f"  supersede {k + 1}/{pairs}: {seconds[-1]:.3f}s", flush=True)
    ordered = sorted(seconds)
    return {
        "notes": notes, "note_bytes": note_bytes, "pairs_timed": pairs,
        "seconds": seconds,
        # The first call absorbs one-off warm-up (page cache, sqlite prepares),
        # so the MEDIAN is what a run of 1360 actually pays.
        "median_seconds": ordered[len(ordered) // 2],
        "rebuild_seconds": round(rebuild_s, 1),
        "load_average_before": list(load_before),
        "load_average_after": list(os.getloadavg()),
    }


# --------------------------------------------------------- the round trip ---

def _frontmatter_blocks(vault: Path) -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8").split("---\n")[1]
            for p in sorted((vault / "brain").rglob("cosbridge-*.md"))}


def roundtrip_fixture() -> dict:
    """Apply the whole plan on a bridge fixture, undo it, diff the frontmatter.

    The ONE residual is the keeper's own ``is_latest_version: true``.
    ``core.unsupersede`` leaves the successor's copy of that key on purpose —
    its docstring says the key may head an unrelated chain — and a bare ``true``
    is valid under ``tools/validate.py``'s ``check_bitemporal`` (only ``false``
    demands a ``superseded_by``). Reported rather than hidden.
    """
    from tools import cos_bridge_retire_duplicates as retire

    tmp = _throwaway("retire-roundtrip-")
    vault = write_bridge_fixture(tmp / "vault")
    core = _core_over(tmp, vault)

    before = _frontmatter_blocks(vault)
    receipt = retire.build_receipt(vault, None)
    applied = retire.replay(core, receipt["apply"], "supersede", reason="DD-02 roundtrip")
    mid = _frontmatter_blocks(vault)
    undone = retire.replay(core, receipt["undo"], "unsupersede", reason="DD-02 roundtrip")
    after = _frontmatter_blocks(vault)

    keepers = {g["keeper"] for g in receipt["groups"] if g["retirees"]}
    identical = sorted(n for n in before if after[n] == before[n])
    residual = {n: [ln for ln in after[n].splitlines()
                    if ln not in before[n].splitlines()]
                for n in before if after[n] != before[n]}
    return {
        "fixture_notes": len(before),
        "groups": len(receipt["groups"]),
        "pairs": len(receipt["apply"]),
        "applied": applied,
        "undone": undone,
        "apply_actually_changed_the_vault": mid != before,
        "byte_identical_after_undo": identical,
        "not_byte_identical_after_undo": residual,
        "retirees_all_byte_identical": all(
            after[n] == before[n] for n in before if n not in keepers),
        "verdict": (
            f"{len(identical)} of {len(before)} notes came back byte-identical. "
            f"The {len(residual)} that did not are the group keepers, each "
            f"carrying exactly one extra line, is_latest_version: true, which "
            f"core.unsupersede leaves on the successor by design."
            if all(v == ["is_latest_version: true"] for v in residual.values())
            else "UNEXPECTED residual after undo — read "
                 "not_byte_identical_after_undo before applying anything."),
    }
