# `docs/security/` — what may live here

**Everything in `docs/` ships.** The public export builds from `git ls-files`,
so a tracked file here reaches `github.com/Autopsias/brainiac` at the next
release. Write for that audience or do not write here.

## The rule

A security document belongs here only if it names **no** client, counterparty,
vendor, engagement or internal codename. `vuln-3385-risk-reduction.md` is the
worked example: it describes a finding and its fix in engine terms alone.

Anything that names one of those is **owner-private** and goes in the plan
directory that produced it — `_plans/<plan>/`, which `.gitignore` covers whole
(`.gitignore:192`), so nothing under it is tracked and nothing under it can
ship. It also keeps the document beside the work it describes.

Moved on 2026-09-04, for exactly that reason:

| file | now at |
|---|---|
| `security-closure-brief-2026-09.html` | `_plans/security-followup-2026-09-01/` |
| `security-findings-ledger-2026-09-02.html` | same |
| `security-findings-ledger-2026-09-02.md` | same |

They named the client's information-security team, a named vendor and its test
id. They had never been committed, so nothing leaked — but an untracked file in
`docs/` is one `git add -A` away from a release.

## Before you commit anything here

```bash
git status --porcelain docs/security/
```

The pre-commit hook `no staged file carries a denylisted client name` is a
floor, not a seal: it matches DECLARED terms. It cannot see a paraphrase, an
undeclared codename, or a description that identifies an engagement without
naming it. Read the file yourself.
