# `tool notes`

Three CLI tools for the hardcoded Obsidian vault at
`~/iCloud~md~obsidian/Documents/Notes`:

- **`clean`** — move unreferenced `z-images/` files, duplicate notes, and empty
  notes to the Trash. Dry run by default; `--apply` is recoverable from Trash.
- **`localize`** — download remote `![alt](https://...)` images into
  `z-images/` as WebP and rewrite the embeds. Dry run by default; `--apply`
  downloads and rewrites.
- **`spellcheck`** — review exact, high-confidence mechanical typo proposals
  one note at a time. It is local and deterministic; it does not initialize a
  model, contact a network service, or make free-form edits.

```bash
uv run tool notes spellcheck
uv run tool notes spellcheck --vault ~/Notes --state ~/.local/state/tool/notes.sqlite3
uv run tool notes spellcheck decisions --state ~/.local/state/tool/notes.sqlite3
uv run tool notes spellcheck decisions --state ~/.local/state/tool/notes.sqlite3 --remove 12
uv run tool notes clean
uv run tool notes clean --apply
uv run tool notes localize
uv run tool notes localize --apply
make fmtlint
uv run pytest tests/notes
```
`localize` positional paths are optional scope limiters: relative paths resolve
against the current directory, falling back to the vault; missing paths and
paths outside the vault are errors. Spellcheck always processes the vault in
stable relative-path order.

## Layout

```
src/tool/notes/
├── common.py       # vault path, shared errors, image conventions
├── clean.py        # image reference and duplicate-note cleanup
├── localize.py     # remote-image parser, downloader, WebP rewrite
├── markdown.py     # protected-region scanner and prose ranges
├── detector.py     # pinned typos-dict loader and exact mechanical detector
├── state.py        # SQLite decisions, cache, and operation journal
├── spellcheck.py   # lazy note review, exact edits, scopes, undo, and CLI
└── data/
    ├── typos_words.csv
    └── TYPOS-LICENSE-{MIT,APACHE}
```

## `clean`

`clean` lists files directly inside `z-images/` and shells out to `rg` for one
fixed-string reference scan. It excludes `.trash` and `.obsidian`. `--apply`
moves files with the system `trash` command, so the operation remains
recoverable from the Trash. Duplicate and empty-note cleanup uses exact file
bytes and keeps the shortest vault-relative path, with alphabetical tie
breaking.

## `localize`

`localize` handles inline `![alt](https://...)` image embeds outside fenced and
inline code. Local paths, data URLs, HTML image tags, reference-style images,
and known video/social page URLs remain unchanged. Downloads use bounded
retries, reject oversized/non-image responses, and store content-addressed
WebP files. Failed downloads leave their original embeds intact.

## `spellcheck`

### Scope and detector

Spellcheck is intentionally a **mechanical-spelling-only** checker. It emits a
proposal only when the pinned curated dataset contains an exact misspelling and
one unambiguous replacement. It does not generate candidates from unfamiliar
words, word frequency, grammar, style, agreement, pluralization, punctuation,
case-only changes, or semantic context. It never splits or joins words.

The vendored data is `typos-dict` 0.14.1 from the `crate-ci/typos` project,
source commit `67e851f0011378f3c32597df0225931cb6ee56a` (the
`typos-dict-v0.14.1` tag), with SHA-256
`9f527a304e4ea717a4dc1b6fc5f3a4d50b8c872f17792097e28422bb595226db`.
Rows with multiple replacements, such as `aache,ache,cache`, are treated as
ambiguous and ignored. The source is dual-licensed MIT or Apache-2.0; both
license texts and attribution are stored beside the data. Refreshing the list
is a deliberate source update, never a runtime download.

Lowercase, title-case, and uppercase source forms are separate exact mappings.
Mixed-case forms abstain. Approving `recieve → receive` never approves
`Recieve → Receive` or any other spelling implicitly.

The detector returns occurrence-level records:

```text
vault-relative note identity
Unicode source range [start:end)
exact original and replacement
detector provenance
exact file snapshot hash
```

Python Unicode code-point offsets are used throughout. Source bytes are read
and decoded without newline normalization, and untouched bytes are preserved.

### Markdown and explicit protection

Existing Markdown protection remains the first exclusion layer. It protects
front matter, fenced and inline code, URLs, link destinations, wikilink
targets, embeds, HTML, math, paths, and indented code while keeping visible
link labels eligible. The detector also requires complete tokens and refuses
matches embedded in identifiers or combining-mark sequences.

The `d` action adds an explicit exact-case protection for a word or containing
phrase. A phrase is stored as one phrase, not as independent words. Scope is
chosen only in this secondary interaction:

- current note (the default)
- current directory subtree
- whole vault

Protections suppress proposals; they do not modify the detector dataset or
create permanent autocorrection rules.

### Review flow and keybindings

Notes are read and analyzed lazily in stable order, so the first note can be
reviewed before later notes are detected. Repeated occurrences of one exact
mapping are grouped only inside the current note:

```text
reading.md

  42  ... I still haven't recieved the book ...
  58  ... recieved another copy ...

recieved → received
2 eligible occurrence(s) in this note
Reason: known mechanical misspelling

[a] apply in note  [A] apply across vault
[n] reject here   [d] remember…
[u] undo          [q] quit
```

- **`a`** applies the displayed mapping to the currently eligible exact
  occurrences in this note. It does not affect another note or create a rule.
- **`A`** scans other notes on demand using the detector and builds a frozen
  occurrence-level plan. The preview shows source/replacement, every planned
  location and context, file/occurrence counts, and exclusions from saved
  decisions. Press `A` again or answer `yes` to confirm one multi-file batch.
  Cancellation records nothing. `A` means apply this plan now, not autocorrect
  future text.
- **`n`** rejects the exact `original → replacement` pair in the current note.
  The decision covers repeated instances, survives unrelated edits, does not
  become global, and does not suppress another replacement for the same
  original.
- **`d`** remembers an exact word or phrase as legitimate, then asks for the
  explicit scope above. It is a protection decision, not a dictionary entry.
- **`u`** undoes the latest completed or partially completed mutating action,
  including a decision action. A vault batch is one logical undo operation.
- **`q`**, EOF, interruption, and unknown input leave completed work intact.
  Unknown keys reprompt and never become rejection decisions. TTY raw mode is
  restored on every exit, and uppercase `A` is preserved in TTY and line-input
  paths.

### Persistence and decision management

State is stored in one private SQLite database, outside the vault by default:

- macOS: `~/Library/Application Support/tool/notes.sqlite3`
- other systems: `$XDG_STATE_HOME/tool/notes.sqlite3`, or
  `~/.local/state/tool/notes.sqlite3`
- override with `--state` or `TOOL_NOTES_STATE`

The database contains separate tables for rejected proposal pairs, accepted
word/phrase protections, deterministic findings cache, and operation
journaling. Note identities are vault-relative paths. Renaming a note does not
transfer its note-local decisions; a same-named file in another directory is a
different identity. Directory and vault scopes apply only when explicitly
selected. More-specific rejection decisions override broader protections.

Inspect or remove active decisions with:

```bash
tool notes spellcheck decisions --state PATH
tool notes spellcheck decisions --state PATH --remove DECISION_ID
```

Removing a decision makes the relevant cached finding eligible again. The
database is private because the cache and journal can contain note-derived
text and complete before/after file bytes.

### Cache, resume, undo, and conflicts

Every note has a cache key containing its relative identity, exact content
hash, detector version, and parser/policy version. Empty findings are cached
too. An unchanged, fully reviewed vault therefore reuses detector results and
does not repeat prompts. Editing one note invalidates only that note's cache
entry. User decisions remain when the detector version changes.

Before writing, every planned range is checked against the reviewed snapshot,
the exact original source, protection rules, and non-overlap constraints. Each
file is replaced atomically with its original permissions and newline bytes.
The operation journal records before/after bytes and which files completed;
this is atomic per file, not a claim of multi-file filesystem transactions.

If a vault batch is interrupted after partial completion, the journal remains
recoverable and the next invocation reports it. `u` restores completed files
only when their current bytes still match the journaled after-snapshot. An
external edit causes undo or apply to stop rather than overwrite it, and the
affected proposal is surfaced again after re-reading the note.

### Requirements and limitations

No Apple Foundation Models runtime, macOS Intelligence feature, spellchecking
service, model dependency, or network access is required. The `apple-fm-sdk`
dependency is not part of this command's installation requirements. The first
version deliberately favors precision over recall: if the curated dataset
does not provide one unambiguous mechanical correction, it abstains. Broader
dictionary generation and contextual checking are future capabilities, not
part of this workflow.

The relevant deterministic suite uses fake detector boundaries for state and
write tests and also exercises the real vendored dataset:

```bash
uv run pytest tests/notes
```
