from tool.notes import spellcheck
from tool.notes.cli import build_parser
from tool.notes.common import VAULT


def test_notes_help_names_subcommands() -> None:
    text = build_parser().format_help()
    assert "clean" in text
    assert "localize" in text
    assert "spellcheck" in text


def test_clean_defaults_to_dry_run() -> None:
    args = build_parser().parse_args(["clean"])
    assert args.apply is False
    assert hasattr(args, "handler")


def test_localize_defaults_to_dry_run() -> None:
    args = build_parser().parse_args(["localize"])
    assert args.apply is False
    assert args.jobs == 8
    assert args.quality == 85
    assert hasattr(args, "handler")


def test_spellcheck_operates_on_vault_without_positional() -> None:
    args = build_parser().parse_args(["spellcheck"])
    assert args.vault == VAULT
    assert args.handler is spellcheck.run


def test_tool_cli_names_notes() -> None:
    from tool import cli

    assert "notes" in cli.build_parser().format_help()
