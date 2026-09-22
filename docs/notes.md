# `tool notes`

Three CLI tools for the hardcoded Obsidian vault at
`~/iCloud~md~obsidian/Documents/Notes`:

- **`clean`** — move files in `z-images/` that no markdown note references,
  plus duplicate and empty notes, to the Trash. Dry run by default;
  `--apply` deletes via the system `trash` command (recoverable from the
  Trash).
- **`localize`** — download remote `![alt](https://...)` images into
  `z-images/` as WebP and rewrite the embeds to `![[<sha>.webp]]`. Dry run by
  default; `--apply` downloads and rewrites.
- **`spellcheck`** — interactively clean spelling across the whole vault
  with Apple's on-device Foundation Models model. The model discovers likely
  typos, repeated mappings are grouped into one candidate each, you approve
  each mapping (`a` fixes that spelling everywhere safe), and up to three
  discovery passes run until no new candidates appear.

```bash
uv run tool notes spellcheck                    # scan vault, review each typo mapping
uv run tool notes clean                       # dry run: list unreferenced images + duplicate notes
uv run tool notes clean --apply               # move them to the Trash
uv run tool notes localize                    # dry run: list remote images
uv run tool notes localize --apply            # download + convert + rewrite
uv run tool notes localize --apply Clippings/  # limit to a note or directory
make fmtlint
uv run pytest tests/notes
```

`localize` positional paths are optional scope limiters: relative paths
resolve against the current directory, falling back to the vault; missing
paths and paths outside the vault are errors, never silent no-ops.

## Layout

```
src/tool/notes/
├── __init__.py         # empty
├── common.py           # hardcoded VAULT, z-images/ name, WebP quality, NotesError
├── clean.py            # rg reference scan, md5 duplicate scan, trash deletion, run()
├── localize.py         # remote-image parser, downloader, ffmpeg WebP, rewriter, run()
├── markdown.py         # protected-region scanner, checkable blocks, range splitting
├── foundation_models.py # apple-fm-sdk adapter: instructions, guided schema, ModelClient
├── spellcheck.py       # vault corpus, discovery, grouping, expansion, review, run()
└── cli.py              # configure_parser()/build_parser() for clean + localize + spellcheck
```

## `clean` reference detection

`clean` lists files directly inside `z-images/` (the webp-paste attachment
folder; flat by convention) and shells out to `rg` — Python never scans note
contents. All basenames go into a patterns file and one
`rg -o -F -f patterns -g '*.md'` pass reports which names occur anywhere in
the vault. Plain fixed-string matching covers every Obsidian reference style:

- `![[abc.webp]]`, `![[abc.webp|300]]`, `[[abc.webp]]`
- `![](z-images/abc.webp)`, `![alt](abc.webp)`
- folder-prefixed `![[z-images/abc.webp]]` (what Loupe imports emit)

Files whose exact basename (with extension) appears nowhere are unreferenced.
Extensionless `[[note]]` links are note links, not image references, and do
not keep images alive. The vault's `.trash` and `.obsidian` directories are
excluded: a note rotting in `.trash` must not pin its images.

`--apply` moves files with the system `trash` command (`/usr/bin/trash`,
macOS 15+) in chunks of 500, so deletions are recoverable.

## `clean` duplicate detection

Non-empty `*.md` files are grouped by md5 of their exact bytes; empty
(0-byte) notes are listed separately and every one is trashed. In each
duplicate group the survivor is the shortest vault-relative path, ties
broken alphabetically — so `hackernews.md` is kept over `hackernews 2.md`
and `Custom Linux Kernel.md`, and `dev/Linux/Oasis Linux Server.md` is
kept over its `archive/` copy. The vault's `.trash` and `.obsidian`
directories are excluded from the scan, same as the image reference scan.

## `localize` scope and naming

Scope matches the webp-paste plugin's Loupe import
(`~/d/obsidian-webp-paste`, `src/loupe-import.ts`): inline
`![alt](destination)` embeds with absolute `http(s)` destinations. Out of
scope, left byte-for-byte intact: `data:` URLs, local paths, HTML
`<img src>`, reference-style images, and anything inside fenced code blocks
or inline code spans (detected with a line-based fence tracker plus
best-effort per-line backtick pairing; exotic nested-backtick cases may slip
through — check dry-run output).

Video and social *page* URLs are also skipped, never attempted: clipping
pipelines sometimes serialize a video as `![](https://www.youtube.com/watch?v=…)`,
and those routes never serve image bytes. `is_page_url()` matches only
routes that provably never serve images — YouTube `watch`/`shorts`/`live`/
`embed`/`playlist`/`clips`, `youtu.be`, `vimeo.com`/`player.vimeo.com` pages,
and X/Twitter status and `/i/` routes — while thumbnails on dedicated image
hosts (`i.ytimg.com`, `i.vimeocdn.com`, `pbs.twimg.com`) stay localizable.
Skipped page embeds are listed separately in dry-run output so a
misclassification stays visible.

Downloads send the plugin's browser-like headers (`Accept: image/...`,
Chrome `User-Agent`) with 3 attempts and exponential backoff for transient
network errors and HTTP 408/425/429/5xx. Responses larger than 100 MB are
refused; responses served as HTML/text are skipped without further work;
anything else is handed to the converter, whose failure also leaves the
embed remote.

## WebP conventions

Follow `~/d/obsidian-webp-paste`:

- Quality 85 (`--quality` overrides, 1–100), lossy for every source format.
- Already-WebP bytes (RIFF `....WEBP` magic) are stored as-is, never
  re-encoded — a lossy re-encode would only shed quality.
- Conversion runs `ffmpeg -c:v libwebp -quality 85` (libwebp is the same
  encoder family as the plugin's Chromium canvas path and `cwebp -q 85`).
- The filename is `sha256(final webp bytes).hexdigest() + ".webp"`, so
  identical images deduplicate to one file and reruns converge.
- The rewrite format is `![[<sha>.webp]]` (bare filename, no `z-images/`
  prefix, no size suffix), matching existing vault embeds.

`--apply` downloads unique URLs concurrently (`-j/--jobs`, default 8) and
stores each image with an atomic temp-file-plus-rename so a crash can never
leave a truncated file behind under a valid hash name. Existing files are
never rewritten (same hash implies identical bytes), then each note is
rescanned and rewritten, preserving everything else byte-for-byte. Partial
localization is normal: failures keep their remote embeds and are reported
as `skipped <url> (<reason>)`.

## `spellcheck` interactive vault cleanup

```bash
uv run tool notes spellcheck
```

Scans every `*.md` note in the vault (excluding `.trash/` and `.obsidian/`),
asks Apple's on-device Foundation Models model to discover likely spelling
mistakes, groups repeated mistakes into one candidate per
`original → replacement` mapping, and lets you adjudicate each candidate
interactively. Approving a mapping (`a`) fixes that spelling in all eligible
prose throughout the vault at once, without further AI calls. Up to three
discovery passes run until a full pass yields no new candidates.

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
```

Actions per candidate: `a` replaces all safe occurrences of the mapping
immediately and continues; `y` replaces only the first shown occurrence and
re-presents the rest; `s` ignores that source spelling for the rest of the
run (ephemeral — nothing is persisted); `n` skips just that mapping;
`q` quits, leaving already-applied edits in place. Keys are single
keystrokes on a macOS terminal, line input otherwise. Candidates are ordered
by number of safe occurrences, then by independent model evidence — no fake
confidence scores. The session ends with counts of mappings approved,
occurrences corrected, notes modified, candidates skipped, spellings ignored,
and discovery passes.

All linguistic judgment comes from the on-device model; no text leaves the
machine. There is no Hunspell, system spellchecker, dictionary, word list,
NLP package, or cloud fallback. The AI is only a discovery mechanism: it
never rewrites notes, only proposes small exact edits through guided
generation (`@fm.generable`), and every proposal must map back to an exact,
unprotected source range before it counts as evidence. Deterministic code
then expands an approved mapping to every exact safe occurrence, and the
human approves every mapping. Nothing is applied the model did not propose
and you did not approve.

The layers are deliberately one-directional — document scanning primitives
(`markdown.py`) know nothing about the model, the model adapter
(`foundation_models.py`) knows nothing about the vault, and `spellcheck.py`
owns corpus, discovery, grouping/expansion, review, and mutation. `run()` is
the only synchronous boundary; it drives a clearly separated async
discovery operation and never makes unrelated `tool` commands async.

Note text is **untrusted input to the model**. The instructions tell it to
treat every block as data rather than instructions, but that is defence in
depth, not the guarantee: the real guarantee is that a proposal can only
survive as a validated substring substitution inside unprotected prose, so
no amount of adversarial note content can replace a file. Notes containing
"ignore previous instructions" or "replace every word" are regression-tested
for exactly this.

Protected regions are never touched by discovery or by global replacement:
YAML front matter, fenced code blocks, inline code spans, raw URLs, Markdown
link/image destinations, autolinks, Obsidian wikilink targets and embeds,
HTML markup, math, indented code, and slash-bearing path tokens. Headings,
paragraphs, list text, blockquotes, and link labels stay eligible. Global
replacement additionally respects lexical boundaries, so approving
`recieve → receive` fixes `I recieve mail.` but leaves `recievePacket`
alone. A deterministic minimal-substitution gate also rejects any proposal
that is not a letter-level tweak within the same words (same token count,
every changed token within edit distance 2), so instruction-following
rewrites such as `Qwen3.5 → ChatGPT` can never become candidates even when
the model is suggestible. A markup-signature gate requires the count of
Markdown-significant characters (``_ * ` # ~ [ ] < > | \ $``) to be identical
on both sides of a proposal — a spelling fix changes letters, never
structure. This catches proposals observed from the live model such as
`_frappato_ → frappato`, which would otherwise have passed the edit-distance
gate and silently stripped the author's italics. Ordinary punctuation that is
not Markdown structure is unaffected, so `dont → don't` is still a valid
correction.

Model output is never fuzzy-matched, never "find the closest word", and never
applied against source text that is no longer exactly what the model saw. A
proposal becomes evidence only when it independently proves: its block id
exists in that request, `original` and `replacement` are non-empty and differ,
the claimed occurrence exists verbatim in that block, the resulting range maps
back to the original document, the range intersects no protected region, and
it overlaps no already-accepted edit. Exact duplicates collapse to one edit;
competing replacements for the *same* range are both kept, so grouping can
surface them as separate candidates rather than guessing. Malformed or
partly-bad output can never endanger the rest of a file.

Proposals carry a 1-based `occurrence` rather than character offsets, because a
small on-device model counts words far more reliably than it counts Unicode
offsets. When `original` matches exactly once in the block, the claimed number
is ignored — the exact match is itself the proof, and rejecting a
proven-unique range over a miscounted number would trade real fixes for no
safety gain. When it matches repeatedly, the claimed occurrence must be valid
or the proposal is dropped.

Accepted proposals are then trimmed to the span that actually differs.
The instructions tell the model to quote surrounding words so a short typo
stays unambiguous, and it does — observed live, it returned
`definately wrong receipt arrived too. → definitely wrong receipt arrived
too.` rather than the typo alone. That matters beyond cosmetics: grouping is
keyed on `(original, replacement)`, so the same typo quoted two different ways
would become two candidates and be adjudicated twice, which is precisely what
`tool notes spellcheck` exists to avoid. Whole identical words are stripped
from both ends of an already-validated range, so the trimmed mapping is a
strict sub-span of one that already proved itself.

The model is never shown Markdown it is forbidden to edit. Protected spans are
blanked in place — replaced with spaces of exactly the same length, newlines
preserved — before the block is rendered for the model, so what it reads is
prose and nothing else. This is worth ~15% of the prompt tokens on the current
vault (measured with the real tokenizer: URLs, paths, and link destinations
tokenize poorly, and they are 87% of everything protected), and it removes the
temptation that produced `_frappato_ → frappato`. Same-length blanking is the
whole trick: offsets need no translation table, because a match the model
reports still lands on the identical source range. Fenced code and front matter
are excluded from blocks entirely, so they are never sent at all.

Scope notes: spelling only — the model instructions forbid style, grammar,
punctuation, and capitalization changes, and uncertain cases must be left
unchanged (false positives are worse than misses).

Discovery packs prose from *many* notes into each request rather than asking
about one note at a time. On-device generation costs several seconds per call
almost independently of how much prose the call carries, so a request per note
spends nearly all of its cost on overhead: measured on this host, eight short
notes cost ~68s as eight requests and ~11s as one. Prose is packed across note
boundaries up to the real context window minus measured instruction/schema
cost minus response headroom, with each unit remembering the note it came from.
Proposals are then validated per source note, so a block id belonging to a
neighbouring note in the same request can never be applied to the wrong file.
A request that still overflows is halved and retried, then skipped with a loud
warning rather than aborting the vault run. Chunks run strictly one at a time:
the on-device model is a single serial resource, and concurrent requests were
measured to be slower than sequential ones, not faster. Holding one session
open across requests is also a loss — the accumulated transcript both slows
generation down and lets earlier notes bias later judgments — so every request
gets a fresh session.

There is no inference server to exploit and none is needed: the model already
lives in a persistent system service (`TGOnDeviceInferenceProviderService`) and
session creation costs ~2 ms, so nothing is started or stopped per request. The
runtime is bounded by model throughput, measured at **~146 chars/s** on this
host. That is worth knowing before starting: the vault currently holds ~19M
characters of eligible prose, so a full pass is on the order of tens of hours,
and the three-pass cap multiplies that. Packing cuts the per-request overhead
(~17% of a whole-vault run here, since the median note is already ~2.5k
characters) but cannot change the throughput term. Progress lines report
`n/total chunks checked` so a long run never looks hung. Expect to run this
overnight, and expect it to be worth starting only when you actually want the
vault cleaned.

Set expectations accordingly. False positives are the failure mode the gates
above exist to prevent, and the model does produce them — the markup gate was
added after the live model proposed stripping italics — but every observed
class is now rejected deterministically, and a final live run surfaced only
genuine typos. Recall is the weaker half: the model reliably finds repeated and
obvious misspellings yet still misses some, including plain ones such as
`recieved → received`. Later passes catch some of what earlier ones missed,
which is the point of running more than one. Recall also degrades as more
blocks are packed into a single request, so packing is a deliberate
throughput/recall trade rather than a free win.

One vault at a time (`--vault` overrides the hardcoded default); files keep
their bytes outside accepted edits, newlines and permissions are preserved, and
each modified note is replaced atomically. There is no backup, undo log, or
approval database — the vault is expected to be externally backed up. Requires
macOS 26+ on Apple Silicon with Xcode 26+ and Apple Intelligence enabled
(`apple-fm-sdk` is a macOS-only dependency and imports lazily, so other
`tool` commands and CI tests work everywhere).

The ordinary suite fakes the model and never needs Apple Intelligence. For
real-model evaluation of recall, false positives, and protected-region safety,
there is an opt-in harness that is deliberately excluded from CI because
generative output is not stable across OS/model revisions:

```bash
TOOL_NOTES_SPELLCHECK_LIVE=1 uv run pytest tests/notes/test_spellcheck_live.py -s
```
