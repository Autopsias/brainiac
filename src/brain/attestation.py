"""Is this release the one our own CI published?

A-03 (owner decision, 2026-08-25). The nightly upgrades itself with no human
present, so a compromised index account, a stolen token or a hijacked plugin
marketplace runs install hooks as the owner — with the vault, the index and the
signing key environment all in reach. The post-update `brain doctor` cannot
help: by the time it runs, the hooks have already run.

**What this checks, and what it deliberately does not.** PyPI accepts a PEP 740
attestation ONLY from a Trusted Publisher, and the attestation names the
repository and workflow that built the file. So the question this answers is
narrow and useful: *did this version come out of the same publisher every other
version came out of?* An attacker who steals an upload token cannot forge that;
an attacker who takes over the PyPI account and re-points the publisher can
still publish, but the publisher changes — and that is exactly the change this
refuses to install through.

It is NOT a check that the code is good. An attestation tells you where a file
came from, never whether to trust it (PyPI's own security model says so). If our
CI is compromised, this passes.

**Attended runs are not gated, and the split is by CALLER, not by a flag.**
The risk here is *unattended* execution; an operator who types the update
command is the authority for it. Only `update_ops._maybe_auto_update` — the
nightly fold's path — calls this. `brain update` goes straight to
`brain_update.run_update` and runs the whole chain end to end.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

PROJECT = "brainiac-cli"

#: The one publisher every legitimate release comes from. Hardcoded, not
#: configured: a value an attacker can edit is not a control, and a user
#: installing this project expects it from this project's own repository.
EXPECTED_REPOSITORY = "Autopsias/brainiac"
EXPECTED_WORKFLOW = "pypi-publish.yml"

_TIMEOUT = 15


def _get_json(url: str, timeout: int) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as fh:  # noqa: S310
        return json.loads(fh.read().decode("utf-8"))


def verify_publisher(version: str, *, timeout: int = _TIMEOUT) -> dict[str, Any]:
    """``{ok, reason, publisher}`` for ``version`` on PyPI.

    FAILS CLOSED on every uncertainty — no attestation, an unreachable index, a
    shape we do not recognise. A check that returns "fine" when it could not
    look is worse than no check, and the caller's response to a hold is to skip
    an upgrade, which costs a day, not a release.
    """
    try:
        meta = _get_json(f"https://pypi.org/pypi/{PROJECT}/{version}/json", timeout)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return {"ok": False, "reason": f"could not reach PyPI: {exc}", "publisher": None}

    files = [entry.get("filename") for entry in (meta.get("urls") or [])]
    files = [name for name in files if name]
    if not files:
        return {"ok": False, "reason": f"PyPI lists no files for {version}",
                "publisher": None}

    # EVERY file, not the first one. A release is what `pip install` may pick,
    # and it may pick the sdist — so an attested wheel beside an unattested
    # sdist is not an attested release.
    for name in files:
        # THE INTEGRITY ENDPOINT, not the JSON API's `provenance` field.
        # Measured 2026-08-25 against pypa/sampleproject 4.0.0: the JSON API
        # reports `provenance: null` for BOTH files while this endpoint returns
        # a full attestation bundle for each. Reading the JSON field would have
        # shipped a gate that holds every release forever, including correctly
        # attested ones — a check that cannot pass, which is the same defect as
        # a check that cannot fail.
        url = (f"https://pypi.org/integrity/{PROJECT}/{version}/"
               f"{urllib.parse.quote(name)}/provenance")
        try:
            prov = _get_json(url, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {
                    "ok": False,
                    "reason": (f"{name} carries no attestation — this project "
                               "has published from CI since 0.20.30; a release "
                               "without one did not come from that path"),
                    "publisher": None,
                }
            return {"ok": False, "reason": f"{name}: provenance unreadable: {exc}",
                    "publisher": None}
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            return {"ok": False, "reason": f"{name}: provenance unreadable: {exc}",
                    "publisher": None}
        bundles = prov.get("attestation_bundles") or []
        if not bundles:
            return {"ok": False, "reason": f"{name}: provenance carries no bundle",
                    "publisher": None}
        for bundle in bundles:
            pub = bundle.get("publisher") or {}
            repo, workflow = pub.get("repository"), pub.get("workflow")
            if repo != EXPECTED_REPOSITORY or workflow != EXPECTED_WORKFLOW:
                return {
                    "ok": False,
                    "reason": (f"{name}: published by {repo}/{workflow}, not "
                               f"{EXPECTED_REPOSITORY}/{EXPECTED_WORKFLOW} — the "
                               "trusted publisher changed, which is what an "
                               "account takeover looks like"),
                    "publisher": pub,
                }
    return {
        "ok": True,
        "reason": (f"every file attested to {EXPECTED_REPOSITORY} "
                   f"/{EXPECTED_WORKFLOW}"),
        "publisher": {"repository": EXPECTED_REPOSITORY, "workflow": EXPECTED_WORKFLOW},
    }
