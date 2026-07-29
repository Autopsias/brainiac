# Synthetic named-entity fixture

**named-entity-golden.json** and **named-entity-qrels.json** are the frozen,
publish-safe S01 evaluation family. Their documents live under
**tests/fixtures/named_entity_vault/**; every name is a Contoso, Northwind, or
Fabrikam placeholder.

They are intentionally separate from **eval/golden_set.json** and
**eval/qrels/**, which are ignored owner-vault runtime artifacts. Do not merge
owner terms into this fixture. S02 combines this family with any local private
baseline only in an uncommitted capture workspace, and checks
**eval/runs/ne-family-freeze.json** before and after that work.
