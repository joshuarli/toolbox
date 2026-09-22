The product goal has changed. Keep useful work from the existing single-file implementation, but steer the feature toward its actual end state:

```text
tool notes spellcheck
```

should interactively clean the entire hardcoded Obsidian vault.

Do not throw away good internals already implemented for:

* Markdown safe/protected-region scanning
* exact source mapping
* Foundation Models integration
* guided generation
* correction validation
* atomic file writes
* model availability/error handling
* token-aware chunking

Those remain valuable. The main change is the orchestration and UX.

## New product goal

`tool notes spellcheck` should mean:

> Scan my entire Obsidian vault, use Apple's on-device Foundation Model to discover likely spelling mistakes, group repeated mistakes together, let me adjudicate each candidate interactively, and efficiently apply approved corrections throughout all eligible prose in the vault.

This is intended as a high-leverage one-time cleanup of the vault, not a command I invoke manually on individual Markdown files.

Remove the Markdown-file positional argument from the public `tool notes spellcheck` CLI.

The internal implementation may retain document-level APIs because they are useful abstractions and test seams, but the public command operates on the configured notes vault.

## Core architecture

The desired pipeline is now:

```text
Obsidian vault
    ↓
enumerate Markdown notes
    ↓
identify eligible prose / protected regions
    ↓
Foundation Models inference over coherent prose chunks
    ↓
validated model-proposed spelling corrections
    ↓
group by (original, replacement)
    ↓
deterministically search the entire eligible vault corpus
for every safe occurrence of `original`
    ↓
interactive human review
    ↓
approved mapping applied deterministically across eligible prose
    ↓
continue through remaining candidates
```

The AI is a **discovery mechanism**, not the thing that must enumerate every occurrence.

That distinction is important.

If the model identifies:

```text
recieve → receive
```

in 3 places, but deterministic scanning shows 17 safe occurrences of `recieve` in eligible prose throughout the vault, the interactive candidate should expose all 17 occurrences.

Once I approve that spelling pair globally, apply it to all 17 safe occurrences without further AI calls.

## Candidate-oriented data model

Elevate the implementation above individual source edits.

Conceptually distinguish:

```python
ModelEvidence
    file
    source range
    surrounding context
    original
    replacement

Candidate
    original
    replacement
    model_evidence[]
    vault_occurrences[]
```

The exact types are up to you.

`model_evidence` consists only of source-mapped, validated corrections actually proposed by the model.

`vault_occurrences` is obtained afterward by deterministic scanning for the exact `original` text throughout eligible prose in the entire vault.

Do not require the model to rediscover each occurrence.

## Grouping

Group validated model suggestions by:

```text
(original, replacement)
```

For example:

```text
recieve → receive
seperate → separate
accomodate → accommodate
```

If the model independently proposes the same pair many times, it should become one interactive candidate.

If the same source string produces competing replacements, e.g.:

```text
foo → bar
foo → baz
```

do not merge them into one arbitrary decision. Preserve the distinct candidate mappings and surface them conservatively.

Candidates with conflicting model evidence should be treated as lower-confidence / more ambiguous, not guessed away.

## Whole-vault deterministic fast path

The central UX feature is:

> once a mapping is approved, perform a cheap deterministic replacement of that exact source spelling throughout all eligible prose in the vault.

Do **not** call raw `.replace()` over entire Markdown files.

Reuse the protected-region scanner.

Global approval means:

> replace every exact safe occurrence of `original` with `replacement` in eligible prose only.

It must not alter occurrences inside:

* fenced code
* inline code
* URLs
* Markdown link/image destinations
* Obsidian wikilink targets
* Obsidian embeds
* front matter
* HTML syntax
* math
* other regions already classified as protected by the implementation

Visible human-readable prose such as headings, paragraphs, list items, blockquotes, and link labels should remain eligible.

## Exact-match semantics

Do not mutate arbitrary substrings inside larger identifier-like tokens.

For example, approving:

```text
recieve → receive
```

should ordinarily affect:

```text
I recieve mail.
```

but not automatically mutate something like:

```text
recievePacket
```

unless that exact larger token was itself the candidate.

Implement a small deterministic notion of lexical boundaries sufficient to avoid substring surprises.

This is structural safety, not linguistic spellchecking. Do not introduce dictionaries or handcrafted spelling rules.

Keep behavior Unicode-safe.

Test this thoroughly.

## Interactive review UX

After discovery and grouping, present candidates one at a time.

Something roughly like:

```text
[1/43] recieve → receive
17 eligible occurrences in 9 notes
AI independently flagged 4 occurrences

1. Projects/foo.md:18
   "... I didn't recieve the package until Tuesday."

2. Notes/bar.md:42
   "... once we recieve a valid response ..."

3. Clippings/baz.md:7
   "... never recieved ..."
```

Only show a small representative sample, ideally up to 3 occurrences from distinct files when possible.

Then prompt with compact actions.

At minimum support:

```text
[a] replace all safe occurrences of this mapping throughout the vault
[y] replace the currently shown/first occurrence only
[s] skip this source spelling for the rest of this invocation
[n] skip this candidate and continue
[q] quit cleanly
```

You may refine the exact keys if there is a materially cleaner UX, but keep it minimal and fast to operate.

The overwhelmingly common action for obvious typos should be one keystroke:

```text
a
```

followed immediately by the next candidate.

Do not require Enter after every keystroke if implementing clean single-key input is simple and portable on the intended macOS host. Do not add a dependency merely for this unless justified.

If ordinary `input()` gives a simpler and more robust implementation, that is acceptable.

## Semantics of the actions

### `a` — approve globally

Immediately apply:

```text
original → replacement
```

to every currently safe occurrence throughout the vault.

Do not wait for a final "commit" phase.

After applying it:

* report number of occurrences/files changed;
* suppress that mapping from the remaining candidate queue;
* do not ask about that source spelling again in the current run if the approved mapping is unambiguous.

Maintain an in-memory mapping such as:

```python
approved_global: dict[str, str]
```

or a richer equivalent.

No persistent custom dictionary or approval database is needed.

### `y` — one occurrence only

Apply only the specifically shown occurrence.

Then continue reviewing remaining occurrences/candidates normally.

Keep exact source mapping; never use global string replacement for this action.

### `s` — skip spelling for current run

Suppress further candidates whose source spelling is that exact `original` for the remainder of this invocation.

This is useful for legitimate personal vocabulary.

This state is ephemeral.

Do not persist it as a dictionary.

### `n` — skip candidate

Skip that `(original, replacement)` candidate only.

A different proposed replacement for the same source spelling may still be surfaced unless `s` was chosen.

### `q`

Quit safely.

Already applied edits remain applied.

No rollback is required.

## Ordering

Prioritize high-leverage candidates.

Default ordering should favor:

1. candidates with the largest number of exact safe vault occurrences;
2. then candidates independently identified by the model multiple times;
3. then lower-frequency candidates;
4. singletons toward the end.

Do not invent fake model confidence scores.

Frequency and independent model evidence are concrete observable signals and are enough for ordering.

## Discovery versus expansion

This distinction is a hard requirement:

```text
AI discovers candidate mappings.
Deterministic scanning expands them to all safe vault occurrences.
```

Do not require Foundation Models to return every typo occurrence.

This is one of the main reasons the new architecture is superior.

For example:

```text
AI flags `seperate → separate` once
```

Then Python discovers:

```text
31 eligible exact occurrences across 12 notes
```

The interactive candidate should say so and global approval should fix all 31.

## Whole-vault scanning

Use the existing hardcoded Notes vault via `resolve_vault()` / existing `notes` conventions.

Enumerate `.md` files recursively while respecting whatever exclusions the other notes tooling already establishes.

At minimum avoid operating inside obvious metadata/trash directories such as:

```text
.obsidian/
.trash/
```

unless current repository conventions already define something better.

Make traversal deterministic.

A lexical path sort is fine.

Do not add directory/path arguments to `spellcheck`.

## Performance

The vault may contain many notes.

Avoid unnecessarily invoking the model for tiny fragments one at a time.

Build coherent batches/chunks using the token-counting architecture from the previous implementation.

The model needs enough context to distinguish:

* typos
* proper nouns
* technical vocabulary
* intentionally strange words

but Foundation Models calls should be reasonably efficient.

It is fine for the initial discovery pass to take some time. The interactive approval loop itself should be effectively instantaneous.

Once a source spelling is globally approved or skipped, avoid spending additional model work on it during subsequent passes when practical.

## Multi-pass cleanup

After the interactive candidate queue is exhausted, perform another discovery pass over the now-modified vault.

Reason: generative inference is not exhaustive; after removing obvious/repeated errors, another pass may expose remaining mistakes.

Carry the current invocation's:

* approved mappings
* skipped source spellings
* skipped candidate pairs

into subsequent passes.

If an approved typo somehow occurs again, apply the already-approved mapping automatically without re-prompting.

Continue until either:

* a full pass yields no new actionable candidate mappings; or
* a conservative fixed maximum number of discovery passes is reached.

Use a small cap such as 3 passes to prevent pathological non-convergence caused by model nondeterminism.

Do not repeatedly bother me with already-reviewed candidates.

At the start of each additional pass, output something concise like:

```text
Pass 2: checking for remaining spelling errors...
```

If no new candidates exist:

```text
No new spelling candidates found.
```

and finish.

## Summary

At completion, print a concise session summary, for example:

```text
Spellcheck complete.

42 spelling mappings approved
193 occurrences corrected
27 notes modified
11 candidates skipped
8 source spellings ignored
3 discovery passes
```

Exact wording is up to you.

Keep counts internally rigorous.

## No backup/change-tracking machinery

Do not add:

* Git commits
* backup copies
* undo logs
* transaction journals
* persistent review databases
* patch files
* approval history files

The vault is already externally backed up.

Keep atomic replacement of individual modified files, because that is cheap and protects against partial/truncated writes.

Atomic writing is filesystem correctness, not backup functionality.

## Preserve the existing AI-only principle

All linguistic decisions still come from Apple's Foundation Model.

Do not introduce:

* word dictionaries
* allowlists
* Hunspell
* Harper
* LanguageTool
* spellcheck libraries
* handcrafted typo tables
* frequency-based English detection
* system NSSpellChecker
* cloud fallback

The global replacement machinery is not a spellchecker. It only executes a mapping that was:

1. proposed by the model; and
2. explicitly approved by the user.

Similarly, `s`/ignore state is only ephemeral interactive state. Do not turn it into a persistent custom dictionary.

## Important interaction between protected syntax and global approval

Suppose the vault contains:

```markdown
I recieve the response.

`recieve()`

[[recieve-notes]]

https://example.com/recieve
```

If I approve:

```text
recieve → receive
```

globally, the result must be:

```markdown
I receive the response.

`recieve()`

[[recieve-notes]]

https://example.com/recieve
```

This should be enforced by the same deterministic protection logic already built for the single-file implementation.

## Important interaction with model evidence

Do not globally replace a spelling simply because Python saw it many times.

A candidate mapping must originate from validated model evidence.

Python may expand:

```text
original
```

to all safe vault occurrences only *after* the model proposed the corresponding:

```text
original → replacement
```

and the user approved it.

Do not create mappings by guessing from edit distance, frequency, or heuristics.

## Tests to add or update

Keep all prior deterministic tests that remain applicable.

Change/add coverage for:

### Vault orchestration

* walks all Markdown notes
* ignores excluded metadata directories
* deterministic traversal
* no public positional file argument

### grouping

* same mapping proposed in multiple notes becomes one candidate
* same original + different replacements remain distinct
* duplicate model evidence is deduplicated correctly

### expansion

* one model hit expands to every exact safe occurrence in eligible prose
* protected occurrences are excluded
* identifier substrings are not accidentally rewritten
* Unicode behavior is correct

### interactive decisions

Use an injected reviewer/input interface rather than forcing tests through a real terminal.

Test:

* `a`
* `y`
* `s`
* `n`
* `q`
* global approval immediately mutates all safe occurrences
* skip-source suppresses later candidates
* skipped candidate does not suppress a distinct candidate unless intended
* quit leaves already-applied edits intact

### multi-pass

* later pass can produce new candidates
* already-approved mapping is not prompted again
* ignored source is not prompted again
* candidate queue converges
* maximum pass count stops nondeterministic loops

### summaries/counts

Ensure reported mappings / occurrences / files modified are correct.

## Retain useful single-document internals

Do not overreact to this steering request by deleting good abstractions.

A function resembling:

```python
scan_document(...)
```

or:

```python
discover_corrections(document, model)
```

is still useful.

The architectural hierarchy should now be closer to:

```text
document scanning primitives
        ↓
vault corpus/index
        ↓
AI discovery
        ↓
candidate grouping + deterministic occurrence expansion
        ↓
interactive reviewer
        ↓
vault mutations
```

The public CLI simply sits on top.

## Keep the implementation small

Do not build an interactive framework.

Do not add curses/TUI machinery.

Do not add persistent state.

Do not create a generic corpus-processing abstraction.

Do not create an event bus.

Do not create a generic AI provider system.

This should still feel like a focused personal CLI tool.

The feature is conceptually:

```text
find likely typo
show me evidence
I approve it
fix that spelling everywhere safe
next
```

Make that path exceptionally clean.

## Update docs

Rewrite the new `spellcheck` section of `docs/notes.md` to document the final UX:

```bash
tool notes spellcheck
```

Explain:

* whole-vault operation;
* Apple on-device Foundation Models discovery;
* interactive grouping by typo mapping;
* global safe replacement;
* Markdown/code/URL protection;
* multiple discovery passes;
* ephemeral approve/skip state;
* immediate writes;
* no traditional spelling dictionary;
* no cloud;
* no backup/undo feature;
* vault is expected to be externally backed up.

Do not document the obsolete single-file public UX.

## Verification

When done:

```bash
make fmtlint
uv run pytest tests/notes
uv run pytest
```

Then exercise the real CLI against a representative disposable copy or controlled fixture vault if changing the hardcoded real vault during development would be unsafe.

If testing against the actual vault, do not perform global writes merely to prove the feature unless it is clearly safe to do so. Real-model dry discovery and mocked/intercepted interactive application are sufficient during development.

## Final standard

The desired user experience is approximately:

```text
$ tool notes spellcheck

Scanning 2,481 notes...
Checking prose with Apple Foundation Models...

[1/61] recieve → receive
17 eligible occurrences in 9 notes
AI flagged this independently in 4 contexts

  Projects/foo.md
    "... didn't recieve the response ..."

  Notes/bar.md
    "... once we recieve it ..."

  Clippings/baz.md
    "... never recieved ..."

[a] all  [y] one  [n] next  [s] skip spelling  [q] quit
> a

Replaced 17 occurrences in 9 notes.

[2/61] seperate → separate
...
```

After the queue:

```text
Pass 2: checking for remaining spelling errors...
Found 7 new candidates.
```

And ultimately:

```text
Spellcheck complete.

42 mappings approved
193 occurrences corrected
27 notes modified
11 candidates skipped
8 spellings ignored
3 discovery passes
```

Implement toward this final shape now, while reusing as much of the already-completed single-file work as remains sound.
