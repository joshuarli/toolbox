.PHONY: fmtlint test

fmtlint:
	uvx ruff@latest check --fix --unsafe-fixes
	uvx ruff@latest format
	uvx ty@latest check

test:
	# joshuarli-pytest-parallel (dev dependency) auto-shards across cpu_count workers
	uv run pytest
