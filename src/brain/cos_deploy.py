"""Which chief-of-staff SKILL.md actually EXECUTES — the one lane-resolution.

Extracted from ``tools/cos_deployed_version.py`` (2026-07-31, STA-01) because
two callers now need it and a second copy of the lane rules is exactly the
drift this module was written to stop:

  * ``tools/cos_deployed_version.py`` — the operator-facing readback, gate for
    the calibration pin.
  * ``brain.cos.write_run_manifest``  — the host's IMMUTABLE record, at run
    LAUNCH, of which bundle produced a run's candidates. ``claim_drops`` fires
    hourly and the deployed skill can change between a run and the claim of its
    output, so "what is deployed NOW" must never stamp a proposal.

"The deployment" is not one thing. Two surfaces can execute the COS nightly and
they hold DIFFERENT versions:

  codex-automation  A Codex automation whose prompt names a SKILL.md path
                    verbatim ("Read and execute <path> end to end"). That file
                    IS what runs. Live lane as of 2026-07-26.
  cowork-desktop    A bundle uploaded into Claude Desktop's session skill store
                    via the owner-only "Save skill" click.

On 2026-07-31 the readback read ONLY the Desktop store while the Codex lane was
the execution path and answered ``MISMATCH … Do NOT move the calibration pin``
against a healthy v5.38 deployment. A readback pointed at the wrong surface is
worse than none, so this module REFUSES rather than guesses: lane resolution is
``--lane`` > an ACTIVE Codex automation naming an existing SKILL.md > refuse.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re
from pathlib import Path

KERNEL_RE = re.compile(r'^\s*kernel_version:\s*["\']([^"\']+)["\']', re.MULTILINE)
EXT_RE = re.compile(r'^\s*extraction_rules_version:\s*["\']([^"\']+)["\']', re.MULTILINE)

LANE_CODEX = "codex-automation"
LANE_COWORK = "cowork-desktop"
LANES = (LANE_CODEX, LANE_COWORK)

CODEX_AUTOMATIONS = Path.home() / ".codex" / "automations"
DESKTOP_SESSIONS = (
    Path.home() / "Library" / "Application Support" / "Claude"
    / "local-agent-mode-sessions"
)

# The automation prompt embeds the path inline ("Read and execute\n/abs/.../
# SKILL.md\nend to end"), so this reads the raw TOML text rather than parsing
# it — tomllib is 3.11+ and this repo supports 3.9.
_SKILL_PATH_RE = re.compile(r"(/[^\s\"'\\]*SKILL\.md)")
_STATUS_RE = re.compile(r'^\s*status\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)

# BOUNDED, not `**`. A Cowork session dir also holds the mirrored workspace, so
# a recursive glob walks the whole vault (and every run's output) on every
# invocation — it hung outright once a live session had written a night of
# artifacts. These are the two shapes Claude Desktop actually uses.
STORE_GLOBS = (
    "*/*/rpm/plugin_*/skills/*chief-of-staff*/SKILL.md",
    "*/*/*/skills/*chief-of-staff*/SKILL.md",
    "*/*/skills/*chief-of-staff*/SKILL.md",
)


class LaneUnresolved(RuntimeError):
    """No surface could be proven to execute the nightly — refuse, never guess."""


class SurfaceUnsupported(LaneUnresolved):
    """The named surface is RETIRED as a version source — see ``cowork_support``.

    A subclass of :class:`LaneUnresolved` on purpose: every existing caller
    already refuses to stamp anything on an unresolved lane, and "this surface
    may not answer" is the same refusal with a different reason.
    """


def _mtime(p: Path) -> str:
    return _dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")


def from_codex_automations(root: Path | None = None) -> list[dict]:
    """Every Codex automation that names a SKILL.md, + that file's version.

    The automation prompt is a DIRECTIVE — "Read and execute <path>" — so the
    named file is the deployment for this lane, whatever any other surface
    holds. `.bak-*` copies are ignored: they are history, not config.
    """
    root = CODEX_AUTOMATIONS if root is None else root
    if not root.exists():
        return []
    out = []
    for toml in sorted(root.glob("*/automation.toml")):
        try:
            text = toml.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sm = _STATUS_RE.search(text)
        status = sm.group(1).upper() if sm else "UNKNOWN"
        for raw in dict.fromkeys(_SKILL_PATH_RE.findall(text)):
            skill = Path(raw)
            entry = {
                "source": "codex-automation",
                "lane": LANE_CODEX,
                "authority": "deployment",
                "path": str(skill),
                "file": str(skill),
                "automation": toml.parent.name,
                "status": status,
                "skill_exists": skill.is_file(),
                "mtime": None,
                "version": None,
                "extraction_rules_version": None,
            }
            if entry["skill_exists"]:
                stext = skill.read_text(encoding="utf-8", errors="replace")
                km, em = KERNEL_RE.search(stext), EXT_RE.search(stext)
                entry["mtime"] = _mtime(skill)
                entry["version"] = km.group(1) if km else None
                entry["extraction_rules_version"] = em.group(1) if em else None
            out.append(entry)
    return out


def from_skill_store(root: Path | None = None) -> list[dict]:
    """Every chief-of-staff-shaped SKILL.md Claude Desktop has on disk."""
    root = DESKTOP_SESSIONS if root is None else root
    if not root.exists():
        return []
    out = []
    seen: set[Path] = set()
    paths = [p for g in STORE_GLOBS for p in root.glob(g)
             if not (p in seen or seen.add(p))]
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        km, em = KERNEL_RE.search(text), EXT_RE.search(text)
        out.append({
            "source": "skill-store",
            "lane": LANE_COWORK,
            # Best-effort even when this IS the resolved lane: the path carries
            # per-session UUIDs with no stable pointer to "the live one" (the
            # same caveat `brain doctor` prints for the plugin store).
            "authority": "deployment (best-effort)",
            "path": str(p.relative_to(root)),
            "file": str(p),
            "skill_exists": True,
            "mtime": _mtime(p),
            "version": km.group(1) if km else None,
            "extraction_rules_version": em.group(1) if em else None,
        })
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def resolve_lane(explicit: str | None, codex: list[dict]) -> tuple[str | None, str]:
    """Which surface actually executes the nightly? Refuse rather than guess."""
    if explicit:
        return explicit, "operator-specified via --lane"
    live = [e for e in codex if e["status"] == "ACTIVE" and e["skill_exists"]]
    if live:
        names = ", ".join(sorted({e["automation"] for e in live}))
        return LANE_CODEX, (
            f"an ACTIVE Codex automation ({names}) names a SKILL.md verbatim; "
            "that file is what gets executed")
    return None, (
        "cannot tell which surface executes the nightly. No ACTIVE Codex "
        "automation names an existing SKILL.md, and a Claude Desktop "
        "skill-store entry only proves an upload happened once — never that "
        "the store is what runs. Re-run with "
        f"--lane {LANE_COWORK} (or --lane {LANE_CODEX}) to assert it.")


def cowork_support(store: list[dict], codex: list[dict]) -> dict:
    """May the Claude Desktop skill store answer "what version is deployed"?

    DEP-03. The Desktop store is not the executing surface for the live
    Codex-automation lane, and reading it as one produced TWO false freeze
    alarms (2026-07-26 and 2026-07-31): both times a tool read v5.29 out of the
    store while the executing mirror was many versions ahead, and both times
    the remediation it manufactured — "do NOT move the calibration pin" — was
    the opposite of correct.

    Documenting that does not retire it: a stale bundle in the store stays
    runnable the moment the owner opens Desktop, and any tool that reads a
    version out of it keeps looking authoritative. So while an ACTIVE Codex
    automation exists and the store disagrees with it, the store is REFUSED as
    a version source rather than answered from. An owner who re-uploads the
    current bundle flips this back to supported with no code change — that,
    or the refusal, is the reconciliation.

    Returns ``{"supported": bool, "reason": str, "executing": [...],
    "store_versions": [...]}``.
    """
    live = [e for e in codex if e.get("status") == "ACTIVE" and e.get("skill_exists")]
    executing = sorted({e["version"] for e in live if e.get("version")})
    store_versions = sorted({e["version"] for e in store if e.get("version")})
    base = {"executing": executing, "store_versions": store_versions}
    if not live:
        return {**base, "supported": True, "reason": (
            "no ACTIVE Codex automation — nothing contradicts the Desktop "
            "store, so it may still be asserted as the deployment")}
    if store_versions and set(store_versions) <= set(executing):
        return {**base, "supported": True, "reason": (
            "the Desktop store holds exactly what the executing "
            f"Codex lane holds ({', '.join(executing)})")}
    return {**base, "supported": False, "reason": (
        f"the {LANE_COWORK} surface is RETIRED as a version source: an ACTIVE "
        f"Codex automation executes {', '.join(executing) or '(unversioned)'} "
        f"while this store holds {', '.join(store_versions) or '(no bundle)'}. "
        "It does not execute the nightly, and two false freeze alarms came from "
        "reading it as if it did. Re-upload the current bundle in Claude "
        "Desktop to make it answerable again, or read the executing lane "
        f"(--lane {LANE_CODEX}).")}


def read_skill(path: Path | str) -> dict:
    """Digest + both versions of ONE SKILL.md, read from the file itself."""
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    km, em = KERNEL_RE.search(text), EXT_RE.search(text)
    return {
        "path": str(p),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "bundle_version": km.group(1) if km else None,
        "extraction_rules_version": em.group(1) if em else None,
    }


def deployed_skill(*, lane: str | None = None) -> dict:
    """The SKILL.md the EXECUTING lane will load, with its digest + versions.

    Raises :class:`LaneUnresolved` rather than answering from a surface that
    does not execute, or from two ACTIVE automations naming different files —
    an ambiguous deployment is not a deployment, and a manifest stamped from a
    guess is worse than no manifest (it looks authoritative).
    """
    codex = from_codex_automations()
    resolved, why = resolve_lane(lane, codex)
    if resolved is None:
        raise LaneUnresolved(why)
    store = from_skill_store() if resolved == LANE_COWORK else []
    if resolved == LANE_COWORK:
        support = cowork_support(store, codex)
        if not support["supported"]:
            raise SurfaceUnsupported(support["reason"])
    entries = [e for e in {LANE_CODEX: codex, LANE_COWORK: store}.get(resolved, [])
               if e.get("skill_exists")]
    if resolved == LANE_CODEX:
        entries = [e for e in entries if e.get("status") == "ACTIVE"] or entries
    files = sorted({e["file"] for e in entries})
    if not files:
        raise LaneUnresolved(
            f"lane {resolved} resolved ({why}) but names no readable SKILL.md")
    if len(files) > 1:
        raise LaneUnresolved(
            f"lane {resolved} names {len(files)} different SKILL.md files "
            f"({', '.join(files)}) — an ambiguous deployment cannot stamp a run")
    return {"lane": resolved, "lane_reason": why, **read_skill(files[0])}
