---
overlay_type: cos
setting: tenant-domains
title: "COS tenant domains — starter template"
updated: 2026-08-15
---

# COS tenant domains

The owner's own mail domains, as an EXACT list. `tools/cos_ground.py` reads
this file to decide each thread's sender class, which decides how much of the
vault a night spends on that thread (grounding design D7/D7a).

**This is a RELEVANCE AND COST heuristic, not a security control.** The sender
string is the `From` header, which the sender writes, and no authenticated
signal (SPF/DKIM/DMARC) is reachable on the enumeration transport. A forged
tenant address classes internal. What contains a hostile sender is the model
leg's capability set — zero tools, zero injected context — plus zero-send by
construction; this list narrows what the host puts in front of a leg that is
already contained. Nothing may cite it as a wall.

**Absent, the night declares itself `ungrounded`** with the reason
`tenant-domains overlay missing: sender classes cannot be computed`. That is
deliberate: without the list every sender classes external, grounding is a
shadow of itself, and a run must say so rather than quietly ship the L1-only
lookup and call itself grounded.

One list line per domain:

```
- example.com
```

Rules the reader enforces, so an entry behaves as written:

- **Exact equality, never a suffix match.** `example.com` does NOT make
  `mail.example.com` internal, and a suffix matcher would make
  `evil-example.com` internal. List every host you mean.
- A leading `@` is stripped; a leading `.` is REJECTED (this is not a suffix
  list). Entries are NFC-normalized and casefolded.
- Only literal ASCII `[a-z0-9.-]` labels. A Unicode homograph domain is refused
  outright, and an `xn--…` label is compared literally — so it can only ever
  match a tenant domain written in that same literal form.
- A malformed entry is a WARNING and is DROPPED (the same fail-closed posture
  `ingest.md` documents): an unparseable rule never infers the permissive
  answer. The dropped entries are named in the run's grounding artifact.

<!-- examples (replace with your own domains, one per line):
- example.com
- example.co.uk
-->
