"""Visible, non-automatic security and refused-draft remediation routes."""


def security_banners(remedy, banner):
    """Build routes using the registry's canonical Remedy type and class."""
    return {
        "read-log:bulk": remedy(
            banner, note="SEC-06: review bulk access records; authorized import and "
                         "maintenance may explain them, but automation must not "
                         "suppress the evidence or decide an incident is harmless"),
        "injection:conceal": remedy(
            banner, note="SEC-05: inspect concealed-instruction findings through "
                         "the classification-gated integrity report; never "
                         "automatically retire sources or silence the finding"),
        "stuck-drafts": remedy(
            banner, note="UPD-01: capture drafts the drain keeps refusing "
                         "(>48h in the inbox) — the skip reason names the fix "
                         "(`brain sync` to read it; `update-needs-owner` applies "
                         "with `brain write`). No branch may auto-apply refused "
                         "untrusted bytes, so this may never route to automation"),
    }
