.PHONY: fmtlint

fmtlint:
	uvx ruff@latest check --fix --unsafe-fixes
	uvx ruff@latest format
	uvx ty@latest check
