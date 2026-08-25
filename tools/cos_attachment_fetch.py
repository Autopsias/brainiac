#!/usr/bin/env python3
"""Fetch attachment BYTES into the host staging directory the sweep reads.

WHY THIS EXISTS. `ingest_sweep` claims files out of `$BRAIN_COS_DOWNLOADS_DIR`
by the names the bridge's ingest-manifest lines carry. Nothing has ever put a
file there: the last ingest manifest is 2026-07-17, and
`.brain/cos/host/attachments/` has been empty since 2026-07-31. The v5.38
(ING-06) design triggered an IN-BROWSER download and hoped it landed in that
directory; it did not, and could not be made to without reconfiguring the
browser profile's download behaviour on every run.

So the bytes come back the way every other read does — `GetAttachment` over
the run's own captured `service.svc` envelope — and the HOST writes the file.
A response cannot land in the wrong folder. There is no
`Browser.setDownloadBehavior`, no `download_status: "landed-elsewhere"`, and
no shared `~/Downloads` anywhere in the path.

THE SELECTION IS THE HOST'S. The page is handed a list of attachment ids and
fetches exactly those, in order. It never decides which files are worth the
bytes — that decision belongs to the judge's category and the owner's
taxonomy lane, upstream of here.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cos_driver_capture import _await_run  # noqa: E402
from cos_driver_transport import DriverStop, open_tab  # noqa: E402

#: One attachment above this is not fetched. A mailbox holds video and disk
#: images; a night's reading budget is not the place to discover that. The
#: page checks the DECODED length before it hands anything back, so an
#: oversized part costs one response, not one download.
MAX_BYTES = int(os.environ.get("BRAIN_COS_ATTACHMENT_MAX_BYTES") or 25_000_000)


def staging_dir() -> Path:
    """The sweep's own directory, resolved the sweep's own way — never
    `~/Downloads`, which `ingest_sweep` refuses by design."""
    configured = os.environ.get("BRAIN_COS_DOWNLOADS_DIR")
    if not configured:
        raise DriverStop(
            "BRAIN_COS_DOWNLOADS_DIR is unset, so there is nowhere the host "
            "sweep would read a file from. The attachment lane is BLOCKED, "
            "not failed: set it to a dedicated host-only staging directory "
            "(the scheduled job carries one in its plist)")
    d = Path(configured).expanduser()
    if d.is_symlink() or d.resolve() == (Path.home() / "Downloads").resolve():
        raise DriverStop(
            f"{d} is shared or symlinked ~/Downloads, which `ingest_sweep` "
            "refuses to sweep — writing there would stage a file nothing "
            "would ever claim")
    d.mkdir(parents=True, exist_ok=True)
    return d.resolve()


def _safe_name(name: str) -> str:
    """The ONE bare-name rule, applied before a filename reaches the disk.

    `cos._safe_basename` is what the manifest line was written through, so a
    name that survives here is the same name the sweep will look for. A part
    named `../../etc/hosts` writes nothing at all.
    """
    from brain import cos  # noqa: PLC0415
    return cos._safe_basename(str(name or ""))


def write_one(dest: Path, name: str, content_b64: str) -> dict[str, Any]:
    """Decode and write ONE part, reporting what actually landed.

    The size and digest are computed from the BYTES ON DISK, re-read, never
    from the buffer that was written. A check that reads its own input proves
    only that the variable did not change between two lines.
    """
    safe = _safe_name(name)
    if not safe:
        return {"filename": name, "written": False, "reason": "unsafe-name"}
    raw = base64.b64decode(content_b64, validate=True)
    path = dest / safe
    # REFUSE, never overwrite (2026-08-25, Codex cloud security round). Manifest
    # lines are keyed by conversation AND filename, so two different messages in
    # one run may legitimately both offer `invoice.pdf`. This wrote both to the
    # same path, and the later sweep then matches candidates BY FILENAME plus a
    # freshness and approximate-size check — so the second message's bytes were
    # claimed under the first message's provenance and classification, and the
    # first attachment was silently lost before its mail was archived.
    #
    # Refusing is the fix rather than disambiguating the name: the sweep looks
    # the file up by the manifest's filename, so a renamed file would go
    # unclaimed and read as a clean run. A refusal is visible, keeps the bytes
    # already on disk bound to the message that actually sent them, and the
    # caller already handles a `written: False` part (see `unsafe-name` above).
    if path.exists() and path.read_bytes() != raw:
        return {"filename": safe, "written": False, "reason": "name-collision",
                "detail": ("another message in this run already staged a "
                           "different file under this name; provenance would "
                           "be ambiguous")}
    path.write_bytes(raw)
    got = path.read_bytes()
    return {"filename": safe, "written": True, "bytes": len(got),
            "sha256": hashlib.sha256(got).hexdigest(),
            "path": str(path)}


def fetch(requests: list[dict[str, str]], *, use_ego: bool = True,
          tab_id: int | None = None, max_bytes: int = MAX_BYTES,
          poll_seconds: float = 1.0, max_wait: float = 300.0,
          dest: Path | None = None) -> dict[str, Any]:
    """`[{attachment_id, filename}] -> report`. Writes into the staging dir."""
    dest = dest or staging_dir()
    # A REQUEST WITH NO ID NEVER REACHES THE PAGE. An empty `Id` is a call
    # that fails at the server and reads like a transport fault; the honest
    # report is that the join could not identify the file.
    unknown = [r for r in requests if not str(r.get("attachment_id") or "")]
    requests = [r for r in requests if str(r.get("attachment_id") or "")]
    ids = [str(r["attachment_id"]) for r in requests]
    if not ids:
        return {"requested": len(unknown), "written": 0, "dest": str(dest),
                "files": [{"filename": r.get("filename") or "", "written": False,
                           "reason": "attachment-id-unknown"} for r in unknown]}
    tab, transport = open_tab(tab_id, use_ego=use_ego)
    res = _await_run(tab, 9001, {"ids": ids, "max_bytes": max_bytes},
                     poll_seconds, max_wait, action="attachments")
    got = res["out"].get("attachments") or []
    by_id = {str(a.get("attachment_id")): a for a in got}

    files: list[dict[str, Any]] = []
    for req in requests:
        a = by_id.get(str(req["attachment_id"])) or {}
        # The page's own name is preferred: it is what the mail server calls
        # the part. The requested name is the fallback, never an override.
        name = a.get("name") or req.get("filename") or ""
        if not a.get("ok") or not a.get("content"):
            files.append({"filename": name, "written": False,
                          "reason": a.get("error") or a.get("code")
                          or "no-content"})
            continue
        files.append(write_one(dest, name, a["content"]))
    files += [{"filename": r.get("filename") or "", "written": False,
               "reason": "attachment-id-unknown"} for r in unknown]
    return {"requested": len(ids) + len(unknown),
            "written": sum(1 for f in files if f["written"]),
            "dest": str(dest), "transport": transport, "files": files}


def requests_for_run(vault: Path, run_id: str) -> list[dict[str, str]]:
    """The files THIS RUN'S MANIFEST LINES CLAIM — never every part it saw.

    The selection belongs to the bridge, which already applied the owner's
    taxonomy lane and the rule-1¾ category: a file with no manifest line is a
    file nobody asked for, and fetching it would spend a night's bytes on
    material the sweep would then have no line to claim. So this joins the
    OTHER way round — manifest line first, ledger row second — and a line
    whose ledger row names no matching attachment id is REPORTED, not guessed
    at (`attachment-id-unknown`); the manifest already refuses to name a file
    it cannot back, and this refuses to fetch one it cannot identify.
    """
    from brain import cos  # noqa: PLC0415
    from brain.notes import sha256_text  # noqa: PLC0415

    ledger = Path(vault) / "cos-ops" / f"_cos_ingestion_ledger_{run_id}.jsonl"
    by_key: dict[str, dict[str, str]] = {}
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        cid = str(row.get("conversation_id") or "")
        if not cid:
            continue
        key = f"{run_id}:{sha256_text(cid)[:12]}"
        for a in row.get("attachments") or []:
            name = _safe_name(str(a.get("filename") or ""))
            if name and a.get("attachment_id"):
                by_key[f"{key}/{name}"] = str(a["attachment_id"])

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for mf in sorted(cos.ingest_manifest_dir(vault).glob("manifest-*.jsonl")):
        for line in mf.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if not str(entry.get("msg_key") or "").startswith(f"{run_id}:"):
                continue
            name = _safe_name(str(entry.get("filename") or ""))
            join = f"{entry['msg_key']}/{name}"
            if not name or join in seen:
                continue
            seen.add(join)
            out.append({"attachment_id": by_key.get(join, ""),
                        "filename": name})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--requests",
                     help="JSON file of [{attachment_id, filename}]")
    src.add_argument("--run", help="fetch the files THIS run's manifest lines "
                     "claim (needs --vault)")
    ap.add_argument("--vault", default=os.environ.get("BRAIN_VAULT"))
    ap.add_argument("--ego", action="store_true", default=True)
    ap.add_argument("--tab-id", type=int, default=None)
    ap.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    ap.add_argument("--dest", default=None,
                    help="override the staging directory (probes only)")
    args = ap.parse_args(argv)
    try:
        if args.run:
            if not args.vault:
                raise DriverStop("--run needs --vault (or $BRAIN_VAULT)")
            reqs = requests_for_run(Path(args.vault).expanduser(), args.run)
        else:
            reqs = json.loads(Path(args.requests).read_text(encoding="utf-8"))
    except (OSError, ValueError, DriverStop) as exc:
        print(json.dumps({"stopped": str(exc)}, indent=2))
        return 2
    # NOTHING CLAIMED IS A CLEAN OUTCOME, not a failure: most nights carry no
    # file-lane candidate at all, and exiting non-zero there would kill every
    # such night on a lane that had nothing to do.
    if not reqs:
        print(json.dumps({"requested": 0, "written": 0, "files": [],
                          "status": "nothing-claimed"}, indent=2))
        return 0
    try:
        report = fetch(reqs, use_ego=args.ego, tab_id=args.tab_id,
                       max_bytes=args.max_bytes,
                       dest=Path(args.dest).expanduser() if args.dest else None)
    except DriverStop as exc:
        print(json.dumps({"stopped": str(exc)}, indent=2))
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["written"] == report["requested"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
