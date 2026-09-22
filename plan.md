You are working in the `joshuarli/tool` repository, specifically `tool notes spellcheck`.

The current spellchecker is still too noisy. It flags valid French words, names, places, titles, technical terms, and other legitimate text. I want you to improve the actual spellchecking core so it generalizes much better across messy real-world personal notes.

The main goal is simple:

> Make `tool notes spellcheck` quiet, fast, multilingual, and trustworthy. False positives are much worse than missed typos.

Do not ask me to choose libraries or manually benchmark approaches. Investigate a few promising strategies yourself, try them on my actual notes, and use your judgment to build the best practical system.

You have full access to my notes vault:

`/Users/josh/Library/Mobile Documents/iCloud~md~obsidian/Documents/Notes`

I have backups. You may inspect and experiment against the real vault. Be reasonably careful, but do not build elaborate safety infrastructure just because the data is personal. Move fast.

## First, understand what exists

Read the relevant repository code and tests, especially:

* `AGENTS.md`
* `src/tool/notes/spellcheck.py`
* `src/tool/notes/markdown.py`
* current spellcheck persistence/cache code
* `docs/notes.md`
* existing spellcheck tests

Run the existing tests before making changes.

Preserve useful existing behavior such as:

* Markdown/code/URL protection
* exact source edits
* note-oriented review
* `a`, `A`, `n`, dictionary/protection actions
* undo
* persistent decisions
* caching

Do not preserve old architecture merely because it exists.

## Figure out the best spellchecking core

Try a small number of serious candidates and combinations. At minimum investigate:

* macOS `NSSpellChecker` via PyObjC
* LanguageTool locally
* CSpell and its dictionaries
* the existing known-typo approach
* Hunspell/Nuspell if useful

Also use something better if you find it.

Do not spend days building adapters for everything. Prototype enough to answer:

1. Does it avoid false positives on my actual notes?
2. Does it handle mixed English/French reasonably?
3. Does it leave names and places alone?
4. Does it still catch obvious typos?
5. Is it fast enough?
6. Is the dependency burden acceptable?

Then pick the best solution and implement it.

A hybrid is fine if it clearly helps. For example:

```
known obvious typo
    ↓
multilingual dictionary/system spellchecker
    ↓
conservative filtering
    ↓
review UI
```

But do not build an elaborate ensemble without evidence that it helps.

## Use my real notes as the benchmark

This is important.

Run candidate approaches over the actual vault and inspect what they flag.

The current problem is false positives, so look closely at findings involving:

* French
* names
* geographic locations
* film/book/music titles
* companies and brands
* technical terms
* unusual capitalization
* foreign words
* slang

Examples that should not get stupid corrections include:

```
Human Resource by Nawapol Thamrongrattanarit
François Truffaut
Château de Chambord
Île-de-France
Reykjavík
São Tomé
io_uring
pyproject.toml
Qwen3.5
aarch64
```

Do not solve these by hardcoding those exact examples.

Also make sure obvious mistakes still work:

```
recieve → receive
enviroment → environment
```

And ideally French mistakes such as:

```
bonjor → bonjour
```

if the selected core can do that reliably.

You may create temporary scripts or debug commands to compare approaches. Delete throwaway experimentation afterward unless it remains useful.

Do not invent precision percentages unless you actually label enough examples to support them. Practical inspection of the real output is sufficient for choosing an approach.

## Important behavior

The fundamental rule is:

> Unknown does not mean misspelled.

If a word might reasonably be a name, place, foreign word, title, technical term, or other legitimate vocabulary, prefer doing nothing.

Do not turn this into grammar correction or copyediting.

For example, do not suggest:

```
Human Resource → Human Resources
```

Do not normalize style, punctuation, capitalization, wording, or grammar.

Real-word mistakes such as:

```
I copied it form the document.
```

are optional. Do not make the whole checker noisier just to catch this class.

## Mixed languages

The notes can contain English and French in the same sentence or paragraph.

For example:

```
I visited Château de Chambord and it was magnifique.
```

This should not generate a pile of English spelling errors.

You do not need perfect language identification.

A union-of-valid-vocabularies approach may be entirely reasonable:

```
valid in English
OR valid in French
OR valid proper noun
OR accepted by system vocabulary
OR accepted by my custom dictionary
→ leave it alone
```

That may create some false negatives. That is acceptable.

Prefer low noise over maximum recall.

Do not require language annotations on each note.

If a simple language configuration is useful, something like English + French defaults is fine.

## Names and places

This is another major source of false positives.

Use whatever practical source works best:

* macOS system dictionaries
* proper-name dictionaries
* geographic vocabulary
* lightweight heuristics
* existing spellchecker knowledge

Do not ignore all capitalized words.

Do not add a huge complicated NER pipeline unless experiments show it is actually necessary.

If `NSSpellChecker` already knows many names and places, take advantage of that instead of rebuilding the world.

## Keep the interactive UX

The existing note-oriented interactive flow should remain simple.

Roughly:

```
typo → correction

[a] apply in this note
[A] apply across vault
[n] reject
[d] remember
[u] undo
[q] quit
```

`A` should only be available or sensible for clearly reusable spelling corrections.

Do not allow a contextual or dubious correction to become a global text replacement.

Persistent user decisions should continue to override generic spellchecker behavior.

Do not overcomplicate scopes or decision metadata beyond what is already useful.

## Separate the checker from the UI

Refactor enough that spellchecking itself returns structured findings rather than printing or reading input directly.

Something roughly like:

```
analyze_note(path, text) -> list[Finding]
```

A finding should contain practical information such as:

* path
* start/end
* original
* replacement
* line/column if convenient
* reason/source
* detected language if known
* whether it is safe for `A`

Do not create a framework around this. A dataclass is probably enough.

The same findings should feed both:

1. the interactive reviewer
2. structured/debug output

## Add structured output

This is required.

I want a non-interactive way to inspect exactly what the checker thinks.

Support something simple like:

```
tool notes spellcheck --format json
tool notes spellcheck --format jsonl
tool notes spellcheck --format diff
```

Choose the exact CLI that fits the project.

No schema versioning. No elaborate compatibility promises. This is an internal tool and can evolve.

JSON/JSONL should include enough to understand each finding:

```
{
  "path": "travel.md",
  "start": 123,
  "end": 129,
  "original": "bonjor",
  "replacement": "bonjour",
  "reason": "french spelling",
  "language": "fr",
  "bulk_safe": true
}
```

Use whatever fields are actually useful.

Structured mode must:

* not enter the interactive UI
* not modify notes by default
* not emit ANSI junk
* keep stdout machine-readable
* put progress/debug noise on stderr
* produce deterministic ordering

## Add diff output

I also want an easy way to eyeball proposed changes:

```
tool notes spellcheck --format diff
```

This should show normal readable file diffs and should be dry-run by default.

It does not need fancy color or patch-application semantics.

The purpose is simply:

> Show me exactly what this spellchecker would change.

## Add useful debugging

Make it easy to understand bad decisions.

An optional verbose/debug mode can explain things like:

```
"Chambord"
accepted by NSSpellChecker
```

or:

```
"bonjor"
unknown in English
unknown in French
French candidate: bonjour
```

Do not dump every accepted token during normal operation.

If useful while developing, keep a lightweight way to compare two spellchecking strategies against one note or the vault. But do not turn strategy comparison into a permanent architecture unless it proves genuinely useful.

## Performance

Measure the approaches you seriously consider.

At least get a practical sense of:

* startup time
* time to first findings
* whole-vault scan time
* cached rerun time

Do not obsess over microbenchmarks.

The command should feel fast enough that I am willing to run it regularly.

Avoid spawning a heavy subprocess once per word.

If a local engine needs one process per invocation, that is fine.

## Dependencies

Use best judgment.

This is already a macOS-oriented tool, so using AppKit/PyObjC is completely acceptable if it works well.

Avoid adding Java or Node unless LanguageTool/CSpell is materially better enough to justify it.

Do not keep several spellchecking engines in production just because you tested them.

Pick one core or one small hybrid and delete abandoned experiments.

## Tests

Add focused tests around the final behavior.

At minimum test:

* valid French inside English text is not flagged
* obvious English typo is caught
* French typo is caught if supported
* names and locations are not spuriously corrected
* technical identifiers remain untouched
* `Human Resource` is not pluralized
* grammar/style/case/punctuation rewrites do not appear
* Markdown/code/URL protections still work
* `a`, `A`, `n`, remember, and undo still work
* structured JSON/JSONL works
* diff output works
* structured output does not invoke the TUI
* structured/diff output does not mutate notes
* Unicode offsets are correct
* caching still behaves sensibly

Do not write giant test matrices for hypothetical future features.

## Clean up afterward

Once you choose the winning approach:

* remove abandoned prototype code
* remove unused dependencies
* remove obsolete spellchecking code
* update stale comments/help text/docs
* keep the final implementation understandable

Do not leave three backends and a generic strategy framework unless there is an actual product reason.

## Validation

Run the relevant tests, linting, formatting, and type checks required by the repository.

Also manually exercise the real command against my actual vault in dry-run/structured mode.

You may use the actual vault for interactive testing as well. I have backups.

## Final report

When done, tell me briefly:

* what approaches you actually tried
* what worked badly and why
* what you chose
* how multilingual text and names/places are handled
* what the new structured/diff commands are
* test results
* rough performance on my vault
* remaining limitations

Do not produce a giant design document.

The goal is working software, not architectural ceremony.

Use judgment, experiment directly, and optimize for the actual notes rather than theoretical completeness.
