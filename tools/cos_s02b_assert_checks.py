"""Per-assertion checks for the s02b verifier's ``assertions`` function.

One function per assertion row; each returns ``(ok, detail)`` — ``ok`` may be
``None`` for an UNPROVEN row — with the condition logic and detail strings
byte-identical to the single function they were extracted from.
``cos_s02b_verify.assertions`` keeps its name and module (the tests and the
plan text name it there) and dispatches to these. The module reads
``brain.cos_echecks`` / ``brain.cos_chips`` directly (they are engine modules,
not the parent); the parent-owned constants (``FOUR_CHIPS``,
``READ_NOISE_SIGNAL``) arrive as parameters.
"""
from __future__ import annotations

from typing import Any

from brain import cos_chips, cos_echecks


def four_chips_check(chips: list[dict[str, Any]],
                     four_chips: set[str]) -> tuple[bool, str]:
    """(1) every chip is one of the four AND the (bucket, tier) matrix fired."""
    bad = [c for c in chips if c["chip"] not in four_chips
           or c["chip"] != c["expected_chip"]]
    ok = not bad and bool(chips)
    detail = (f"{len(chips)} dispatched chip(s); "
              + ("no chip write disagrees with the four-chip (bucket, tier) "
                 "matrix" if not bad else
                 f"{len(bad)} disagree: "
                 + str([(c['digest'], c['verdict'], c['judged_tier'], c['chip'],
                         c['expected_chip']) for c in bad[:6]]))
              + ("" if chips else " — ZERO chips is a zero denominator, not a pass"))
    return ok, detail


def archive_eligibility_check(archives: list[dict[str, Any]]) -> tuple[bool, str]:
    """(2) every archive READ + bucket noise, citing a recognized signal."""
    bad = [a for a in archives
           if a["verdict"] != "noise" or a["read_state"] != "read"
           or a["judged_tier"] in ("P0", "P1")
           or a["noise_signal"] not in cos_echecks.ARCHIVING_SIGNALS]
    ok = not bad and bool(archives)
    detail = (f"{len(archives)} archived thread(s); "
              + ("every one was READ, sits in bucket `noise`, is not P0/P1 and "
                 "cites a recognized typed signal" if not bad else
                 f"{len(bad)} breach it: "
                 + str([(a['digest'], a['verdict'], a['read_state'],
                         a['judged_tier'], a['noise_signal']) for a in bad[:6]])))
    return ok, detail


def widening_shipped_check(archives: list[dict[str, Any]], truth_table: Any,
                           read_noise_signal: str) -> tuple[bool, str]:
    """(2b) the archive widening actually shipped, joined against the s02
    TRUTH TABLE.

    THE LABEL IS NOT THE PROOF. A recurring sender with >=3 rows tonight can
    carry the new signal while archiving nothing new, so the new-signal rows
    are joined against the s02 TRUTH TABLE — the independent statement of
    which rows the OLD rules would not have archived.
    """
    newly = set(truth_table.get("newly_archived") or []) \
        if isinstance(truth_table, dict) else set()
    new_signal = [a for a in archives if a["noise_signal"] == read_noise_signal]
    only_new = [a for a in new_signal if a["digest"] in newly]
    ok = bool(only_new)
    detail = (f"{len(new_signal)} archived row(s) carry `{read_noise_signal}`, of "
              f"which {len(only_new)} are named by the s02 truth table as rows "
              f"the OLD rules would NOT have archived "
              f"({sorted(a['digest'] for a in only_new)[:6]}). Zero such rows "
              "means the widening did not ship, which is a miss, not a pass")
    return ok, detail


def validator_branch_check(ledger: list[dict[str, Any]] | None,
                           read_noise_signal: str) -> tuple[bool, str]:
    """(2c) the new signal's VALIDATOR BRANCH must read a field a PRODUCER
    writes — `automated-mail-marker` was retired at run 127 because its
    branch validated against `ctx["automated_marker"]`, which nothing wrote."""
    judge = cos_echecks._cos_judge()
    branch_ok = False
    if judge is not None:
        rule = judge.RULES.get("triage.noise_signal_required")
        v = {"auto_archive": True, "bucket": "noise", "tier": "P3",
             "noise_signal": read_noise_signal}
        refuses = rule.check(v, {"read_state": "unread"}) if rule else "no rule"
        accepts = rule.check(v, {"read_state": "read"}) if rule else "no rule"
        produced = any(r.get("read_state") for r in (ledger or []))
        branch_ok = bool(refuses) and accepts is None and produced
    detail = ("the `triage.noise_signal_required` branch for "
              f"`{read_noise_signal}` refuses on `read_state != read`, accepts on "
              "`read`, and `read_state` is a field the driver actually writes on "
              "the ingestion ledger" if branch_ok else
              "the branch does not discriminate on a field some producer writes "
              "— the run-127 shape")
    return branch_ok, detail


def unread_shield_check(run: dict[str, Any],
                        ledger: list[dict[str, Any]] | None) -> tuple[bool, str]:
    """(3) no unread thread was archived (and none mutated at all)."""
    verdicts = cos_echecks.by_conversation(ledger or [])
    touched_unread = [r.get("conversation_id_digest")
                      for r in cos_echecks.dispatched(run["undo"])
                      if verdicts.get(r.get("conversation_id"), {})
                      .get("read_state") != "read"]
    ok = not touched_unread
    detail = ("no dispatched mutation names a thread the ingestion ledger reports "
              "as anything but READ" if not touched_unread else
              f"{len(touched_unread)} mutated thread(s) were not READ: "
              f"{touched_unread[:6]}")
    return ok, detail


def capability_set_check(man: dict[str, Any], ho: dict[str, Any],
                         ) -> tuple[bool, str]:
    """(6) run-bound capability set, and MANAGED_CHIPS grew by exactly one."""
    host_commit, host_clean = man.get("git_commit"), man.get("git_clean")
    frozen = man.get("capability_digest")
    now = cos_echecks.capability_digest()
    problems = []
    if not host_commit:
        problems.append("the run manifest records no host commit, so today's "
                        "constants cannot be read as last night's")
    if host_clean is not True:
        problems.append(f"the tree was not clean when the run fired "
                        f"(git_clean={host_clean!r})")
    if ho.get("validated_commit") != host_commit:
        problems.append(f"the handoff CLAIMS commit "
                        f"{str(ho.get('validated_commit'))[:12]} while the HOST "
                        f"recorded {str(host_commit)[:12]}")
    if ho.get("worktree_clean") is not host_clean:
        problems.append("the handoff's clean flag disagrees with the host's")
    if not frozen or frozen != now:
        problems.append(f"the capability digest the manifest froze "
                        f"({str(frozen)[:12]}) is not the one this tree hashes "
                        f"to ({str(now)[:12]})")
    if len(cos_chips.CHIPS) != 4:
        problems.append(f"MANAGED_CHIPS holds {len(cos_chips.CHIPS)} name(s), "
                        "not the four the doctrine names")
    ok = not problems
    detail = ("; ".join(problems) if problems else
              f"the capability set at the HOST-RECORDED commit "
              f"{str(host_commit)[:12]} (clean tree) is byte-identical to the "
              f"digest the manifest froze, and MANAGED_CHIPS holds exactly the "
              f"four names")
    return ok, detail


def cap_honoured_check(ho: dict[str, Any],
                       archive_count: int) -> tuple[bool | None, str]:
    """The blast cap — count the APPLIED archives against what the owner set."""
    cap = ho.get("owner_archive_cap")
    if not isinstance(cap, int) or isinstance(cap, bool):
        return None, (f"owner_archive_cap is {cap!r} — UNRECORDED, so the abort rule "
                      "is unenforceable and cannot be verified")
    return archive_count <= cap, (f"{archive_count} archive(s) applied against an "
                                  f"owner cap of {cap}")


def plan_identity_check(plan: dict[str, Any], dry: dict[str, Any],
                        binding: dict[str, Any]) -> tuple[bool, str]:
    """The applied plan's digest is the one its OWN rehearsal and binding named."""
    digest_ok = bool(plan.get("plan_digest")) and \
        plan.get("plan_digest") == dry.get("plan_digest") == \
        binding.get("plan_digest")
    detail = (f"the applied plan's digest {str(plan.get('plan_digest'))[:16]} is "
              f"the digest its OWN rehearsal and binding named (within one run — "
              f"a cross-run comparison is structurally impossible, `plan_digest` "
              f"hashes the run id)" if digest_ok else
              f"plan {str(plan.get('plan_digest'))[:12]}, rehearsal "
              f"{str(dry.get('plan_digest'))[:12]}, binding "
              f"{str(binding.get('plan_digest'))[:12]} do not agree")
    return digest_ok, detail
