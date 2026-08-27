# Contributing

Contributions are welcome. Read the next section first, because how this
repository is published changes how a contribution reaches it.

## This repository is a release mirror

Development happens in a private repository. What you are reading is a
**clean-room export**: each release arrives here as one squashed commit plus a
signed tag, with the whole tree. There is no per-change history to browse, and
that is deliberate rather than an oversight.

Because there is no history, the tag carries the whole claim about where a
release came from — so from `v0.20.31` the release commit and its tag are both
SSH-signed and GitHub reports them as Verified. Check any tag yourself:

```bash
git verify-tag v<version>
```

The reason is in
[`docs/adr/0001-publish-via-clean-room-export.md`](docs/adr/0001-publish-via-clean-room-export.md):
the private history carries a client codename and internal build-session
identifiers in *every* commit. Publishing that history — or rewriting it with
`filter-repo` and hoping — would disclose material that is not ours to
disclose. So the release is the unit of publication.

**You do not have to take that on trust.** The export tooling ships in this
tree:

- [`tools/export_cleanroom.py`](tools/export_cleanroom.py) builds the published
  file list and applies every exclusion.
- [`tools/publish_public.py`](tools/publish_public.py) runs the release
  pipeline that produced this commit.

Run them and compare the result against what is here.

## What is verifiable about a release

Since `v0.20.30`, PyPI artifacts are built and published by GitHub Actions over
OIDC and carry a [PEP 740](https://peps.python.org/pep-0740/) attestation
naming the repository, workflow and commit that produced them. Nothing is built
on a maintainer's laptop and uploaded by hand.

To check a release yourself:

```bash
pip download --no-deps brainiac-cli==<version>
# then, against the same version:
curl "https://pypi.org/integrity/brainiac-cli/<version>/<filename>/provenance"
```

The `publisher` field must read `Autopsias/brainiac` and `pypi-publish.yml`.

## How to contribute

**Issues** — open here. Bug reports, questions and design discussion all
belong on this repository's issue tracker.

**Pull requests** — open here too. They cannot be merged in the usual way,
because `main` only ever moves by release. What happens instead: the change is
carried into the private tree, reviewed and tested there, and lands in the next
release. You keep authorship — the commit carries a `Co-authored-by:` trailer
naming you, and the changelog entry credits you.

That is slower than a merge button and it means your patch appears in a
squashed commit rather than as your own. If that trade is not acceptable to
you, say so on the issue; it is a reasonable objection and worth hearing.

**Security issues** — do not open a public issue. See
[`SECURITY.md`](SECURITY.md) for private vulnerability reporting.

## Where review actually happens

Every change passes, in the private tree, before it can reach a release:

- the full test suite (`pytest`, roughly 4,800 tests);
- `ruff` for correctness lint, and `semgrep` for Python, secrets and workflow
  rules;
- `shellcheck` at error severity on every shell script;
- three quality ratchets (file size, function length, cyclomatic complexity)
  that block any commit making a file worse than every parent commit;
- a client-name scan that refuses any staged file naming a client, including
  names buried inside identifiers and filenames.

CI on *this* repository re-runs the whole-project versions of those checks on
every release push, plus CodeQL, dependency review, and a supply-chain audit.
Their results are public in the Actions tab.

**The honest limitation:** because history is squashed, a third party cannot
inspect the individual review steps — only their outcome, and the artifacts
they produced. If you need per-change auditability, this project does not offer
it today. Say so in an issue if it matters to you; the model is a documented
trade-off, not a conviction.

## Conventions, if you are writing code

Read [`AGENTS.md`](AGENTS.md) first. It is the single conventions file for this
project and it governs note shape, link style, capture rules and the security
posture. If a tool, an agent, or a human disagrees with it about *shape*, it
wins.
