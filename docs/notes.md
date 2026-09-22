# `tool notes`

Three CLI tools for the hardcoded Obsidian vault at
`~/iCloud~md~obsidian/Documents/Notes`:

- **`clean`** — move unreferenced `z-images/` files, duplicate notes, and empty
  notes to the Trash. Dry run by default; `--apply` is recoverable from Trash.
- **`localize`** — download remote `![alt](https://...)` images into
  `z-images/` as WebP and rewrite the embeds. Dry run by default; `--apply`
  downloads and rewrites.
- **`spellcheck`** — review high-confidence spelling proposals one note at a
  time, or print them as JSON/JSONL/diff. It is local (macOS system
  dictionaries plus a curated typo list); it never contacts a network service
  or makes grammar, style, case, or punctuation edits.

```bash
uv run tool notes spellcheck
uv run tool notes spellcheck --format jsonl            # or json / diff; never prompts or writes
uv run tool notes spellcheck --format diff --note travel.md
uv run tool notes spellcheck --format jsonl -v 2>why.txt  # why every unknown word was (not) proposed
uv run tool notes spellcheck explain Chambord bonjor
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
`localize` positional paths and spellcheck `--note` paths are optional scope
limiters: relative paths resolve against the current directory, falling back to
the vault; missing paths and paths outside the vault are errors. Spellcheck
always indexes the whole vault (usage counts are vault-wide) and reviews notes
in stable relative-path order.

## Layout

```
src/tool/notes/
├── common.py       # vault path, shared errors, image conventions
├── clean.py        # image reference and duplicate-note cleanup
├── localize.py     # remote-image parser, downloader, WebP rewrite
├── markdown.py     # protected-region scanner
├── speller.py      # NSSpellChecker adapter (batched word checks, guesses)
├── typos.py        # pinned typos-dict loader
├── checker.py      # spelling core: scan, judge, Finding records
├── state.py        # SQLite decisions, caches, and operation journal
├── spellcheck.py   # note review, structured output, exact edits, undo, CLI
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

### What gets proposed

The rule is **unknown does not mean misspelled**; false positives are worse
than missed typos. A word is left alone when any configured macOS dictionary
accepts it (`--languages`, default `en,en_GB,fr`: the union of American
English, British English, and French, plus every word taught to macOS with
"Learn Spelling"). An unknown word becomes a proposal only through one of two
paths in `checker.SpellChecker.judge`:

1. **Known misspelling** (`bulk_safe`): the pinned `typos-dict` table has an
   entry *and* `NSSpellChecker`'s first guess is the same word. The table alone
   proposes names and foreign words (`sur → sure`, `Hass → Hash`); requiring
   agreement of two independent sources is what removes them.
2. **Single typing slip** (note-local only): the system's first guess is one
   plausible keystroke away (adjacent-key substitution, a doubled or
   neighbouring extra key, a dropped letter, or a swap — never touching the
   first letter), the guess list is short and unambiguous, the unknown form
   appears at most twice in the vault, it is not a known stem plus a productive
   affix (`lagering`, `virality`), and the correction is a word the vault
   already uses (five times for a capitalized word).

Before either path, structure rules out likely names and identifiers:
capitalized words mid-sentence, acronyms and mixed case, tokens joined to
digits, `_`, `.`, `-`, or soft hyphens (`io_uring`, `Qwen3.5`,
`Île-de-France`), standalone emphasized labels (`**username**`), and
fragments of PDF-broken words. Proposals never change only case, accents, or
apostrophes, never split or join words, and never address grammar
(`form`/`from`, `Human Resource`).

Mixed languages need no annotations. Each sentence is attributed to the base
language that most of its words exclusively belong to; French sentences get
French guesses (`bonjor → bonjour`) and `typos-dict` only applies to English
sentences.

### Engines considered

Measured on the real vault (2,189 notes, 13.5 MB): `NSSpellChecker` knows
most names and places (Reykjavík, São Tomé, Truffaut, Château) and French,
respects the user's learned words, and batch-checks ~25 µs/word; its `guesses`
cost ~10 ms/word and serialize in the `AppleSpell` service, so the checker
gates them behind cheap local filters and caches every answer. LanguageTool
needs Java, took 21 s for one 116 KB note, and flags grammar and names.
CSpell needs Node and had similar vocabulary coverage with noisier
suggestions. `typos-dict` alone produced the French/name false positives this
design replaced. Hunspell was not pursued: its dictionaries carry fewer names
than either of the above.

### Findings and structured output

`checker.Finding` is the single record behind both the review and
`--format json|jsonl|diff`: vault-relative `path`, 1-based `line`/`column`,
Unicode code-point `start`/`end`, `original`, `replacement`, `reason`,
sentence `language`, and `bulk_safe`. Structured output applies saved
decisions, is ordered by path then offset, never prompts, never writes notes,
and keeps progress on stderr. `diff` is a plain unified diff of every
proposal. `-v` prints the verdict for every unknown word to stderr, and
`spellcheck explain WORD...` prints one word's full decision trail.

The vendored data is `typos-dict` 0.14.1 from the `crate-ci/typos` project,
source commit `67e851f0011378f3c32597df0225931cb6ee56a` (the
`typos-dict-v0.14.1` tag), with SHA-256
`9f527a304e4ea717a4dc1b6fc5f3a4d50b8c872f17792097e28422bb595226db`.
Rows with multiple replacements are ignored. The source is dual-licensed MIT
or Apache-2.0; both license texts and attribution are stored beside the data.

### Markdown and explicit protection

Existing Markdown protection remains the first exclusion layer. It protects
front matter, fenced and inline code, URLs, link destinations, wikilink
targets, embeds, HTML, math, paths, and indented code while keeping visible
link labels eligible. The review re-verifies these regions and complete-token
boundaries before any edit.

The `d` action adds an explicit exact-case protection for a word or containing
phrase. A phrase is stored as one phrase, not as independent words. Scope is
chosen only in this secondary interaction:

- current note (the default)
- current directory subtree
- whole vault

Protections suppress proposals; they do not modify macOS dictionaries or
create permanent autocorrection rules.

### Review flow and keybindings

The vault is indexed first (cached scans make this fast), then notes are
judged lazily in stable order, so the first note is reviewable before later
notes request system guesses. Repeated occurrences of one exact mapping are
grouped only inside the current note:

```text
reading.md

  42  ... I still haven't recieved the book ...
  58  ... recieved another copy ...

recieved → received
2 eligible occurrence(s) in this note
Reason: known misspelling (typos-dict), confirmed by system spellchecker

[a] apply in note  [A] apply across vault
[n] reject here   [d] remember…
[u] undo          [q] quit
```

- **`a`** applies the displayed mapping to the currently eligible exact
  occurrences in this note. It does not affect another note or create a rule.
- **`A`** is offered only for `bulk_safe` (known-misspelling) proposals; a
  context-dependent slip correction refuses it and re-prompts. It scans other
  notes for the same exact word and builds a frozen
  occurrence-level plan, prints it (source/replacement, every planned location
  and context, file/occurrence counts, exclusions from saved decisions), and
  applies it immediately as one multi-file batch. `A` means apply this plan
  now, not autocorrect future text.
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
word/phrase protections, disposable spelling caches, and operation
journaling. Note identities are vault-relative paths. Renaming a note does not
transfer its note-local decisions; a same-named file in another directory is a
different identity. Directory and vault scopes apply only when explicitly
selected. More-specific rejection decisions override broader protections.

Inspect or remove active decisions with:

```bash
tool notes spellcheck decisions --state PATH
tool notes spellcheck decisions --state PATH --remove DECISION_ID
```

Removing a decision makes the relevant proposal eligible again. The
database is private because the cache and journal can contain note-derived
text and complete before/after file bytes.

### Cache, resume, undo, and conflicts

Three disposable caches live in the state database. `note_scans` keeps one
row per note: its content hash, vault-wide word counts, and unknown prose
tokens with their sentence language, keyed by checker/typos version, macOS
version, learned-words mtime, and languages. `spelling_words` keeps each
word's accepting-language bitmask and `spelling_guesses` each system guess
list. A first run over the vault takes ~35 s (dictionary lookups plus ~1,400
guesses); a warm rerun takes ~3.5 s, and editing one note rescans only that
note. Findings themselves are not cached because vault-wide usage changes
them; judging cached tokens is cheap. User decisions survive cache changes.

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

Requires macOS and `pyobjc-framework-cocoa` for `NSSpellChecker`; no network
access or model. Known limits: lowercase names and product terms the system
dictionary lacks can still be proposed when they look like one slip from a
common word (`hwang → hang`); real-word errors (`form`/`from`) are never
caught; typos whose correction the vault never uses, repeated typos without a
`typos-dict` entry, and typos inside hyphenated compounds are missed; French
proposals need a clearly French sentence.

`tests/notes/test_checker.py` exercises the real system dictionaries (skipped
without AppKit); `tests/notes/test_spellcheck.py` pins the review, journal, and
output contracts with a fake checker:

```bash
uv run pytest tests/notes
```
