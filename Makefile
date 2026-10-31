.PHONY: fmtlint lint test sync-py

fmtlint:
	uv run --locked ruff check src tests
	uv run --locked ty check

lint:
	uv run --locked ruff check src tests

test:
	# joshuarli-pytest-parallel (dev dependency) auto-shards across cpu_count workers
	uv run --locked pytest

# Install the optional browser automation dependency.
sync-py:
	uv sync --extra browser
