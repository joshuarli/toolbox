"""Private SQLite state for the notes spellchecker.

The database deliberately separates durable user decisions from disposable
caches (per-note scans, per-word dictionary verdicts and guesses) and from the
operation journal.  Note identities are
vault-relative POSIX paths; the database never writes metadata into notes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATE_ENV = "TOOL_NOTES_STATE"
SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class Decision:
    id: int
    kind: str
    scope_kind: str
    scope_value: str
    source: str
    replacement: str | None
    matching: str
    provenance: str


@dataclass(frozen=True, slots=True)
class OperationFile:
    note_id: str
    before: bytes
    after: bytes
    before_hash: str
    after_hash: str
    applied: bool


@dataclass(frozen=True, slots=True)
class Operation:
    id: int
    kind: str
    status: str
    metadata: dict[str, Any]
    files: tuple[OperationFile, ...]
    decision_ids: tuple[int, ...]


def sha256_bytes(data: bytes) -> str:
    """Return the stable content identity used by snapshots and cache rows."""
    return hashlib.sha256(data).hexdigest()


def default_state_path() -> Path:
    """Return the private state location without creating it."""
    override = os.environ.get(STATE_ENV)
    if override:
        return Path(override).expanduser()
    if xdg := os.environ.get("XDG_STATE_HOME"):
        return Path(xdg).expanduser() / "tool" / "notes.sqlite3"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/tool/notes.sqlite3"
    return Path.home() / ".local/state/tool/notes.sqlite3"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class StateStore:
    """SQLite-backed decisions, findings cache, and undo journal."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or default_state_path()).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._connection = sqlite3.connect(self.path)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('rejection', 'protection')),
                scope_kind TEXT NOT NULL CHECK (scope_kind IN ('note', 'directory', 'vault')),
                scope_value TEXT NOT NULL,
                source TEXT NOT NULL,
                replacement TEXT,
                matching TEXT NOT NULL,
                provenance TEXT NOT NULL,
                created_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS decisions_active_idx
                ON decisions(active, kind, scope_kind, scope_value);
            DROP TABLE IF EXISTS findings_cache;
            CREATE TABLE IF NOT EXISTS note_scans (
                note_id TEXT PRIMARY KEY,
                scan_key TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS spelling_words (
                word_key TEXT NOT NULL,
                word TEXT NOT NULL,
                accepted INTEGER NOT NULL,
                PRIMARY KEY(word_key, word)
            );
            CREATE TABLE IF NOT EXISTS spelling_guesses (
                speller_key TEXT NOT NULL,
                language TEXT NOT NULL,
                word TEXT NOT NULL,
                guesses TEXT NOT NULL,
                PRIMARY KEY(speller_key, language, word)
            );
            CREATE TABLE IF NOT EXISTS operations (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                metadata TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operation_files (
                operation_id INTEGER NOT NULL REFERENCES operations(id),
                note_id TEXT NOT NULL,
                before_bytes BLOB NOT NULL,
                after_bytes BLOB NOT NULL,
                before_hash TEXT NOT NULL,
                after_hash TEXT NOT NULL,
                applied INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(operation_id, note_id)
            );
            CREATE TABLE IF NOT EXISTS operation_decisions (
                operation_id INTEGER NOT NULL REFERENCES operations(id),
                decision_id INTEGER NOT NULL REFERENCES decisions(id),
                PRIMARY KEY(operation_id, decision_id)
            );
            INSERT OR IGNORE INTO metadata(key, value) VALUES ('schema_version', '1');
            """
        )
        self._connection.commit()
        os.chmod(self.path, 0o600)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def note_scans(self, scan_key: str) -> dict[str, tuple[str, dict[str, Any]]]:
        """Cached scans made with *scan_key*: note id → (content hash, payload)."""
        rows = self._connection.execute(
            "SELECT note_id, content_hash, payload FROM note_scans WHERE scan_key = ?", (scan_key,)
        )
        return {str(note_id): (str(digest), json.loads(payload)) for note_id, digest, payload in rows}

    def put_note_scans(self, scan_key: str, scans: dict[str, tuple[str, dict[str, Any]]]) -> None:
        """Replace each note's single cached scan row."""
        self._connection.executemany(
            "INSERT OR REPLACE INTO note_scans(note_id, scan_key, content_hash, payload) VALUES (?, ?, ?, ?)",
            [(note_id, scan_key, digest, json.dumps(payload)) for note_id, (digest, payload) in scans.items()],
        )
        self._connection.commit()

    def spelling_words(self, word_key: str) -> dict[str, int]:
        """Cached dictionary verdicts: word → bitmask of accepting languages."""
        rows = self._connection.execute("SELECT word, accepted FROM spelling_words WHERE word_key = ?", (word_key,))
        return {str(word): int(accepted) for word, accepted in rows}

    def put_spelling_words(self, word_key: str, verdicts: dict[str, int]) -> None:
        self._connection.executemany(
            "INSERT OR REPLACE INTO spelling_words(word_key, word, accepted) VALUES (?, ?, ?)",
            [(word_key, word, accepted) for word, accepted in verdicts.items()],
        )
        self._connection.commit()

    def spelling_guesses(self, speller_key: str, language: str, word: str) -> list[str] | None:
        row = self._connection.execute(
            "SELECT guesses FROM spelling_guesses WHERE speller_key = ? AND language = ? AND word = ?",
            (speller_key, language, word),
        ).fetchone()
        return None if row is None else [str(guess) for guess in json.loads(row[0])]

    def put_spelling_guesses(self, speller_key: str, language: str, word: str, guesses: list[str]) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO spelling_guesses(speller_key, language, word, guesses) VALUES (?, ?, ?, ?)",
            (speller_key, language, word, json.dumps(guesses)),
        )
        self._connection.commit()

    def active_decisions(self) -> list[Decision]:
        rows = self._connection.execute(
            """
            SELECT id, kind, scope_kind, scope_value, source, replacement, matching, provenance
            FROM decisions WHERE active = 1 ORDER BY id
            """
        ).fetchall()
        return [Decision(*row) for row in rows]

    def add_decision(
        self,
        *,
        kind: str,
        scope_kind: str,
        scope_value: str,
        source: str,
        replacement: str | None = None,
        matching: str = "exact-case",
        provenance: str = "explicit-user-decision",
    ) -> int:
        row = self._connection.execute(
            """
            SELECT id FROM decisions
            WHERE active = 1 AND kind = ? AND scope_kind = ? AND scope_value = ?
              AND source = ? AND replacement IS ? AND matching = ?
            ORDER BY id LIMIT 1
            """,
            (kind, scope_kind, scope_value, source, replacement, matching),
        ).fetchone()
        if row is not None:
            return int(row[0])
        cursor = self._connection.execute(
            """
            INSERT INTO decisions(
                kind, scope_kind, scope_value, source, replacement, matching, provenance, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (kind, scope_kind, scope_value, source, replacement, matching, provenance, _now()),
        )
        self._connection.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def deactivate_decision(self, decision_id: int) -> bool:
        cursor = self._connection.execute("UPDATE decisions SET active = 0 WHERE id = ? AND active = 1", (decision_id,))
        self._connection.commit()
        return cursor.rowcount == 1

    def remove_decisions(self, decision_ids: list[int]) -> int:
        if not decision_ids:
            return 0
        marks = ",".join("?" for _ in decision_ids)
        cursor = self._connection.execute(
            f"UPDATE decisions SET active = 0 WHERE active = 1 AND id IN ({marks})", decision_ids
        )
        self._connection.commit()
        return cursor.rowcount

    def begin_operation(self, kind: str, metadata: dict[str, Any] | None = None) -> int:
        now = _now()
        cursor = self._connection.execute(
            "INSERT INTO operations(kind, status, metadata, created_at, updated_at) VALUES (?, 'started', ?, ?, ?)",
            (kind, json.dumps(metadata or {}), now, now),
        )
        self._connection.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def add_operation_file(
        self,
        operation_id: int,
        note_id: str,
        before: bytes,
        after: bytes,
        *,
        applied: bool = False,
    ) -> None:
        self._connection.execute(
            """
            INSERT OR REPLACE INTO operation_files(
                operation_id, note_id, before_bytes, after_bytes, before_hash, after_hash, applied
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (operation_id, note_id, before, after, sha256_bytes(before), sha256_bytes(after), int(applied)),
        )
        self._connection.commit()

    def mark_operation_file_applied(self, operation_id: int, note_id: str) -> None:
        self._connection.execute(
            "UPDATE operation_files SET applied = 1 WHERE operation_id = ? AND note_id = ?",
            (operation_id, note_id),
        )
        self._connection.commit()

    def link_operation_decision(self, operation_id: int, decision_id: int) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO operation_decisions(operation_id, decision_id) VALUES (?, ?)",
            (operation_id, decision_id),
        )
        self._connection.commit()

    def finish_operation(self, operation_id: int, status: str) -> None:
        self._connection.execute(
            "UPDATE operations SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), operation_id),
        )
        self._connection.commit()

    def _operation(self, row: sqlite3.Row | tuple[Any, ...]) -> Operation:
        operation_id, kind, status, metadata = row
        files = tuple(
            OperationFile(
                note_id=str(file_row[0]),
                before=bytes(file_row[1]),
                after=bytes(file_row[2]),
                before_hash=str(file_row[3]),
                after_hash=str(file_row[4]),
                applied=bool(file_row[5]),
            )
            for file_row in self._connection.execute(
                """
                SELECT note_id, before_bytes, after_bytes, before_hash, after_hash, applied
                FROM operation_files WHERE operation_id = ? ORDER BY note_id
                """,
                (operation_id,),
            )
        )
        decisions = tuple(
            int(decision_row[0])
            for decision_row in self._connection.execute(
                "SELECT decision_id FROM operation_decisions WHERE operation_id = ? ORDER BY decision_id",
                (operation_id,),
            )
        )
        return Operation(int(operation_id), str(kind), str(status), json.loads(metadata), files, decisions)

    def latest_undoable(self) -> Operation | None:
        row = self._connection.execute(
            """
            SELECT id, kind, status, metadata FROM operations
            WHERE status IN ('started', 'partial', 'completed', 'undo-conflict')
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        return None if row is None else self._operation(row)

    def pending_operations(self) -> list[Operation]:
        rows = self._connection.execute(
            "SELECT id, kind, status, metadata FROM operations WHERE status IN ('started', 'partial') ORDER BY id"
        ).fetchall()
        return [self._operation(row) for row in rows]

    def mark_undone(self, operation_id: int) -> None:
        self.finish_operation(operation_id, "undone")

    def mark_undo_conflict(self, operation_id: int) -> None:
        self.finish_operation(operation_id, "undo-conflict")

    def decision(self, decision_id: int) -> Decision | None:
        row = self._connection.execute(
            """
            SELECT id, kind, scope_kind, scope_value, source, replacement, matching, provenance
            FROM decisions WHERE id = ? AND active = 1
            """,
            (decision_id,),
        ).fetchone()
        return None if row is None else Decision(*row)
