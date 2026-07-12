# Brainiac — supply-chain hardening targets (see .github/workflows/supply-chain.yml).
# The lock is the hash-pinned runtime closure a corporate/managed install should
# resolve against instead of pyproject's open `>=` ranges.

LOCK_EXTRAS = --extra mcp --extra ingest --extra embed

.PHONY: lock audit sbom

lock:  ## Regenerate the hash-pinned dependency lock from pyproject.toml
	uv pip compile pyproject.toml $(LOCK_EXTRAS) --generate-hashes -o requirements.lock

audit:  ## Fail on any known CVE in the pinned deps (needs pip-audit)
	pip-audit -r requirements.lock --no-deps --desc

sbom:  ## Emit a CycloneDX SBOM from the lock (needs cyclonedx-bom)
	cyclonedx-py requirements requirements.lock -o sbom.cyclonedx.json
