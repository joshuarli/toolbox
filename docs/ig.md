# `tool ig`

Headless Chromium scraper that downloads Instagram posts and reels, records metadata in SQLite, tracks scanned profile and saved-post membership, can sync following lists, and can build a static local archive site. Browser automation is provided by the reusable `shadowdriver` Python extension; Instagram-specific crawling policy stays in this repo. Install browser support with `uv sync --extra browser`; ordinary `uv sync` leaves it out.

## Source layout

```
src/tool/ig/
  browser.py                - Instagram defaults/adapters over generic shadowdriver browser and crawler primitives
  browser_types.py          - lightweight Protocols for browser/tab-like objects used across scraper modules
  db.py                     - SQLite schema, migrations, and completion predicates
  hq_video.py               - authenticated HQ video metadata (web v1 media info) and DASH manifest selection
  media.py                  - inferred media filename/path helpers
  types.py                  - ScrapedPost and Pace dataclasses
  scraper.py                - direct scrape primitives, media URL validation/selection, downloads, compatibility exports
  post.py                   - single-post orchestration and per-tab scrape/save flow
  video_save.py             - video save strategy: captured segments, HQ tracks, audio muxing, CDN fallback
  web_api.py                - shared authenticated web v1 API helpers (headers, JSON GET, user-id resolution)
  profile_timeline.py       - profile timeline metadata via Instagram web v1 feed API
  profile_scan.py           - profile/saved grid scan and scanned-post download loop
  following.py              - authenticated following-list web v1 sync
  site_builder.py           - static archive site builder (also exposed as `tool ig build`)
  cli/                      - ig subcommands and target classification
  js/                       - browser-side snippets used by scrapers and fingerprint probes
web/assets/                  - generated-site CSS, JS, and icons (repo root, alongside tests/, docs/)
tests/ig/                    - unit tests for scraper, browser helpers, CLI, following sync, profile scan, and site build
```

There is no current `src/tool/ig/profile.py` legacy modal module. Modal-style image collection helpers live in `scraper.py` because direct post pages and Instagram modal DOMs still share useful structure.

## Working in this repo

- **Prefer serial investigation over explore agents.** Do not spawn explore/research subagents to look through `src/tool/ig/`. Read files directly, inline in the conversation. The overhead of agent dispatch and context duplication outweighs any parallelism benefit when files are a few keystrokes away. Regular coding subagents (for independent implementation tasks) are fine.

## Architecture

**`src/tool/ig/browser.py`** owns Instagram's browser policy and delegates generic browser work to `shadowdriver`. Shadowdriver (Chromium/CDP) supplies the default binary and manages the isolated profile location, while this adapter opens tabs, subscribes to generic network events, applies Instagram's request-blocking defaults, and exposes the compatibility helpers used by scraper modules (`browser_session`, `new_tab`, `fetch_bytes`, `navigate`, `human_click`, `human_scroll`, `ProtocolError`, and `BrowserInUseError`). Shadowdriver's Python APIs that touch the browser, processes, timers, filesystem diagnostics, or profile lifecycle are awaitable; keep ig callers async rather than wrapping them in synchronous shims.

The isolated profile is required. The scraper must not use or disturb the user's normal browser profiles. `tool ig kill` delegates to Shadowdriver's managed-profile shutdown helper; it must not kill personal browser windows.

Shadowdriver drives Chromium over CDP and exposes generic command/event/tab helpers. Images are saved through `fetch_bytes()`, which performs a browser-context `fetch(cache: "force-cache")` and returns bytes through the Python extension.

Shadowdriver owns generic browser lifecycle, protocol plumbing, request blocking, browser-backed fetches, cookies, and human input. This repo owns Instagram-specific selectors, CDN URL scoring, rate-limit policy, media persistence, SQLite writes, and site generation. Do not move Instagram-specific behavior into Shadowdriver.

The browser setup applies Shadowdriver's practical compatibility preload: it hides exposed automation surfaces when needed, keeps the document visible, and pins the desktop viewport/screen persona. Prefer native macOS surfaces over broad JavaScript spoofing.

**`src/tool/ig/db.py`** owns SQLite initialization, migrations, and completion predicates:

- `init_db()` creates `post`, `profile_post`, `profile_following`, and `media`. `profile_post.saved` scopes normal profile membership (`0`) separately from saved-post membership (`1`).
- `is_done(...)` is the completion predicate for profile and saved downloads: metadata must exist and all inferred files must be present on disk in the requested membership scope.

**`src/tool/ig/media.py`** owns inferred media filenames and paths. Media filenames are not stored in SQLite; callers infer paths from shortid, post type, index, total media count, and media directory.

**`src/tool/ig/hq_video.py`** owns authenticated HQ video metadata. It reads Instagram cookies from the driven browser through Shadowdriver, converts the shortcode to its numeric media id, and fetches the post from Instagram's web v1 endpoint (`GET https://www.instagram.com/api/v1/media/<media_id>/info/`, following yt-dlp's current logged-in path) instead of the legacy `graphql/query` shortcode query, which Instagram rejects with `{"data": null}`. It parses the returned DASH XML and selects the best video/audio tracks. HTTP 429 is surfaced as `HqVideoFetchError(status=429, retry_after=...)` so orchestration can convert it to profile-download rate limiting.

**`src/tool/ig/scraper.py`** contains shared direct-scrape and save primitives:

- `scrape_direct(tab, shortid, ...)` reads a directly navigated `/p/<shortid>/` page, extracts Relay/script/DOM data, resolves image or video media, and returns `ScrapedPost`.
- `save_post(...)` validates selected media, upserts `post` and `media`, downloads files, and can attach a post to `profile_post` in either normal or saved scope.
- `SegmentCapture`, image/video URL ranking, direct-page readiness, carousel image collection, and browser/Python download helpers live here.
- `scraper.py` intentionally re-exports several DB, media, and HQ-video names for compatibility with existing callers and tests. Prefer importing new code from the owning module (`db.py`, `media.py`, or `hq_video.py`) unless preserving an existing import surface.

`save_post()` validates before writing media rows. Standalone video posts must resolve to one final URL. Instagram image URLs that look like low-quality feed thumbnails fail loudly instead of silently saving poor media.

**`src/tool/ig/post.py`** orchestrates direct post scrapes. `scrape_post()` opens a browser session; `scrape_post_in_tab()` handles one tab so profile downloads can reuse a browser process while opening and closing a fresh tab per shortid. `_maybe_like()` lives here because profile downloads visit direct post pages.

**`src/tool/ig/video_save.py`** owns video persistence strategy. Video saves prefer complete captured browser segments when available, otherwise mux selected video/audio URLs or download the chosen CDN MP4 fallback. HQ DASH tracks are attempted first (see the HQ video path below) and then use the same save path; when HQ resolution fails for any non-rate-limit reason the save falls back to the standard chain with a warning. Keep the ordering of these fallbacks stable unless changing user-visible video behavior intentionally.

**`src/tool/ig/browser_types.py`** defines Protocols for tab-like objects. Use these in new cross-module APIs instead of accepting untyped `tab`/`browser` objects when practical.

**`src/tool/ig/web_api.py`** owns the shared authenticated web v1 API request machinery used by `following.py` and `profile_timeline.py`: cookie-based `auth_headers`, `request_json()` (JSON GET with transient-error retries and HTTP-429 surfacing, parameterized error class), and `profile_id()` (username → numeric user id via `web_profile_info`). The retired legacy `/graphql/query` document queries (shortcode `doc_id` and `query_hash`) must not be reintroduced; the current web app serves these surfaces from `www.instagram.com/api/v1/*`.

**`src/tool/ig/profile_timeline.py`** owns the normal-profile timeline metadata path. It reads the browser cookies, resolves the profile's numeric id via `web_api.profile_id()`, then pages `GET https://www.instagram.com/api/v1/feed/user/<user_id>/?count=12&max_id=...` (`open_profile_timeline()` + `fetch_profile_posts_page()`). Feed items use the same JSON shape the old captured-timeline nodes did, so `post_from_timeline_node()` parses them into `ScrapedPost` metadata with selected image/video URLs. `ProfileTimelineUnavailableError` lets `profile_scan.py` fall back to grid scrolling.

**`src/tool/ig/profile_scan.py`** is the active profile and saved-post workflow. For normal profiles, `scan_profile()` tries the profile timeline metadata path first and stores ordered `profile_post` membership plus complete `post`/`media` metadata without opening individual post pages. If the timeline API is unavailable, it falls back to scrolling the profile grid and recording stubs. Saved scans still navigate `/saved/all-posts/`, scroll without opening post modals, collect visible `/p/<shortid>/` and `/reel/<shortid>/` links, and record saved-scope stubs. `download_scanned_profile()` reads membership in scan order, skips `is_done()` rows, downloads from stored profile metadata when present, and falls back to direct post pages only when metadata is missing.

Failed scanned-post downloads are recorded in `profile_failure` with stage `scan_direct` and the matching saved scope. Re-running `tool ig get @username` or `tool ig get --saved @username` retries incomplete rows and rows with missing media.

**`src/tool/ig/following.py`** syncs a profile's following list. It reads `sessionid` and `csrftoken` through Shadowdriver, resolves the profile id through `web_api.profile_id()`, pages the web v1 friendships endpoint (`GET https://www.instagram.com/api/v1/friendships/<user_id>/following/?count=200&max_id=...`), and stores current/stale followees in `profile_following`.

**`src/tool/ig/cli/`** is the `ig` entry point. Subcommands are split by file:

- `login` - opens a headed isolated Chromium window for Instagram login. Shadowdriver launches it via LaunchServices and surfaces an unexpected browser exit as `BrowserCrashedError` (with singleton-lock evidence and the browser log tail) instead of silently exiting.
- `scan` - records profile grid membership; `--saved` scans saved posts, defaulting to `________emanon________` when no profile is provided; `--full` ignores the stop-at-known shortcut.
- `get` - downloads incomplete scanned profile rows for `@profile`; `--saved` downloads saved-post rows, defaulting to `________emanon________` when no profile is provided; `--live`, `--media-dir`, and `-j/--jobs` are supported.
- `get-post` - downloads a single post by shortid via the direct `/p/<shortid>/` path, independent of any scanned profile. Accepts a bare shortid or a `/p/<shortid>/` / `/reel/<shortid>/` URL. Debug/reproduction tool; supports `--live`, `--media-dir`, and `--pace`.
- `get-following` - fetches following lists for `@profile`; `--dump` prints stored current usernames.
- `info` - prints normal and saved profile counts from SQLite.
- `remove` - deletes one profile's membership rows and inferred media files, with `--dry-run`; `--saved` removes only saved-post membership for that profile.
- `build` - builds the static archive site from the database (`--media-dir` supported).
- `doctor`, `fingerprint`, `kill` - diagnostics, fingerprint probe, and isolated browser shutdown.

Profile download and scan targets are intentionally strict: `tool ig scan` and `tool ig get` accept `@profile` only. Direct single-post scraping is available as the `tool ig get-post <shortid>` debugging command.

**`src/tool/ig/site_builder.py`** builds the static archive under `site/`. It reads `post`, `profile_post`, and `media`, infers media paths from shortid/type/index, copies referenced media into `site/images`, generates WebP thumbnails when ffmpeg is available, writes per-profile JSON data, and renders `index.html` plus one profile HTML page per scanned username. Saved collections render as separate archive keys such as `username-saved.html` and `data/username-saved.json`. Run via `tool ig build`.

## Profile scanning and downloads

Profiles and saved posts use a two-step workflow:

1. `uv run tool ig scan @username` resolves the profile id and pages Instagram's web v1 `feed/user` timeline endpoint, recording full post metadata plus ordered membership rows.

   ```
   post:         shortid=<shortid>, username=<owner>, caption=<caption>, post_type=<image|carousel|video>, scraped_at=<unix seconds>
   media:        shortid=<shortid>, idx=<media index>, url=<selected CDN URL>
   profile_post: username=<profile>, saved=0, shortid=<shortid>, seen_at=<unix seconds>, seq=<profile order>
   ```

   Existing stub rows are backfilled with metadata. A stop-at-known scan does not stop at an already-known shortid if that row still lacks media metadata.

   If the profile timeline API is unavailable, `scan @username` falls back to scrolling the profile grid and inserts newly observed shortids into `post` as metadata stubs and into `profile_post` as ordered membership rows.

   ```
   post:         shortid=<shortid>, username=NULL, caption=NULL, post_type=NULL, scraped_at=NULL
   profile_post: username=<username>, saved=0, shortid=<shortid>, seen_at=<unix seconds>, seq=<profile order>
   ```

   `post.username` is the scraped owner when known, not the scan queue key.

   `uv run tool ig scan --saved [@username]` scans `https://www.instagram.com/<username>/saved/all-posts/` and writes the same kind of stubs with `profile_post.saved=1`. When no username is supplied, saved scan defaults to `________emanon________`, the logged-in account used for iteration. Saved pages may not satisfy the normal profile-grid layout probe, so saved scans wait for either a grid layout or collectable post/reel anchors.

2. `uv run tool ig get @username` reads `profile_post` rows in scan order and downloads rows where `is_done()` is false. For rows with stored media metadata, it keeps a profile tab open and prefers browser-context fetches for those media URLs, avoiding direct `/p/<shortid>/` document navigation. If a browser fetch misses cache or hits CDN CORS limits, the downloader falls back to the direct Python HTTP path with Instagram referer headers. Rows without metadata fall back to direct post pages. `uv run tool ig get --saved [@username]` does the same for saved rows, defaulting to `________emanon________` when no username is supplied; saved rows are usually stubs and therefore commonly use the direct-page fallback. The browser process may stay alive during the command to preserve session state.

   `uv run tool ig get -j <n> @username` may open multiple tabs and download posts concurrently. Keep SQLite writes serialized through the downloader's DB lock. Emit immediate serialized milestone lines per post (page load, scrape, download, mux, DB write, completion) so parallel work is visible, and keep verbose detail output buffered with the same post prefix. Treat HTTP 429 as a global stop signal: the first `RateLimitError` records the triggering post failure, cancels active sibling jobs, closes tabs through normal cleanup, and leaves remaining queued rows pending for a later retry.

This avoids scrolling a virtualized profile grid to resume old work. Pending work is any `profile_post` row whose metadata is incomplete or whose inferred media files are missing.

Normal and saved memberships are intentionally separate: the same `username` and `shortid` can exist once with `saved=0` and once with `saved=1`. Shared `post`, `media`, and inferred media files are keyed by shortid so duplicate downloads can be skipped when the same post appears in both collections.

## Storage

- `instagram.db` - SQLite with WAL mode. Core tables created by `init_db()` are `post`, `profile_post`, `profile_following`, and `media`.
- `profile_post` - ordered membership rows keyed by `(username, saved, shortid)`.
- `profile_failure` - retryable scanned-profile failures, created by the profile scan module and scoped by `saved`.
- `profile_scrape` - legacy compatibility table still created by the profile scan module, but not part of the active scan/download flow.
- `site/images/` - default downloaded media directory. Override with `tool ig get --media-dir <dir>` or `tool ig build --media-dir <dir>`.
- Shadowdriver managed Chromium profile - isolated browser profile, DevTools port file, and `chromium.log`. Use `uv run tool ig doctor` to see the current path.

Media filenames are inferred rather than stored: single images use `{shortid}.jpg`, carousels use `{shortid}_1.jpg`, `{shortid}_2.jpg`, and videos use `{shortid}.mp4`.

## Fingerprinting

The scraper assumes genuine macOS ARM hardware. The intended browser persona is a macOS laptop running Chrome with a Mac-compatible UA, `navigator.platform = "MacIntel"`, Apple-backed WebGL, US English locale, desktop viewport/screen metrics, no touch support, no exposed automation globals, and a foreground-visible document.

This is not a general anti-detect browser. Treat JavaScript getter overrides as a compatibility layer, not as sufficient hardware impersonation. Strong fingerprinting can compare page, worker, service-worker, WebGL, canvas, audio, media-device, font, header, timezone, and host/IP surfaces.

`uv run tool ig fingerprint` intentionally probes more than the values Instagram currently needs. Keep warnings conservative. If `hardwareConcurrency`, `deviceMemory`, viewport, WebGL, locale, timezone, headers, fonts, media devices, automation globals, plugin surfaces, or worker consistency changes, update both the browser setup/probe and tests. Verify both headless and `--live`.

## Running

```
uv run tool ig login
uv run tool ig scan @<username>
uv run tool ig scan --saved [@username]
uv run tool ig scan --full @<username>
uv run tool ig scan --saved --full [@username]
uv run tool ig get @<username>
uv run tool ig get --saved [@username]
uv run tool ig get --live @<username>
uv run tool ig get --media-dir <dir> @<username>
uv run tool ig get -j <jobs> @<username>
uv run tool ig get-post <shortid>
uv run tool ig get-post https://www.instagram.com/p/<shortid>/
uv run tool ig get-following @<username>
uv run tool ig get-following --dump @<username>
uv run tool ig info
uv run tool ig remove [--dry-run] <username>
uv run tool ig remove --saved [--dry-run] <username>
uv run tool ig doctor
uv run tool ig fingerprint
uv run tool ig fingerprint --live
uv run tool ig kill
uv run tool ig build
uv run tool ig build --media-dir <dir>
python build_site.py
make site
```

## Direct-post scraping

`scrape_direct()` is the fallback/debug single-post path. It waits for a direct page signal, reads browser-side scrape data, extracts Relay script media when available, and only falls back to DOM carousel traversal when needed. Normal profile scans and gets should prefer the profile timeline metadata API because direct `/p/<shortid>/` document navigation can return Instagram HTTP 429 even when the same post is reachable through the web app.

Images are saved with `save_post(..., browser_fetch=lambda url: fetch_bytes(tab, url))`, so bytes come from the same browser context and can use its cache/cookies. Videos are selected as one final full MP4. Performance entries and Relay data can include audio-only tracks, alternate encodes, DASH segments, and recommendations; `_best_video_url()` ranks candidates and `save_post()` enforces one video media row.

### HQ video path

Video saves attempt authenticated HQ resolution by default in `save_video_post_media()`. `scrape_direct()` identifies the video post, then `scrape_post_in_tab()` calls into `video_save.py`, which resolves HQ tracks through `hq_video.py` before falling back to normal video save behavior.

The HQ resolver reads Instagram cookies through Shadowdriver's backend-agnostic `tab.get_cookies()` (`Network.getAllCookies`). It requires both `sessionid` and `csrftoken`; missing cookies fail before metadata requests. The web v1 request is authenticated with those cookies and Instagram web headers. HTTP 429 becomes `RateLimitError` so profile downloads record the failure and back off. Other HQ HTTP errors (or a cookie-read `ProtocolError`) fall back to the standard video chain with a warning instead of failing the post, so an unauthenticated or partial save still completes at the best available quality.

## Scraping strategies that did not work

**Direct `/p/<shortid>/` document navigation** - Navigating a fresh tab directly to `https://www.instagram.com/p/<shortid>/` can return a real main-document HTTP 429. This is not just bad detection in our scraper: network events and performance entries show the document response itself is 429, with an empty completed page.

**Browser availability is not proof direct posts are allowed** - The logged-in account can still browse Instagram and load posts via the web app. Profile-grid clicks route inside the single-page app and load post content from existing profile/Relay data; they do not necessarily issue a successful `/p/<shortid>/` document request.

**Synthetic clicks and URL mutation** - Creating an anchor for a target post and clicking it still behaves like direct navigation and can 429. Mutating an existing React-owned Instagram link's `href` is unreliable because React may route using the original internal props. `history.pushState()` can change the address bar but leaves unrelated post content on screen.

**Minimal GraphQL replay** - Posting only a discovered `doc_id` and variables to `/graphql/query` can return HTTP 403, and Instagram later retired these document queries entirely (they answer `{"data": null}` or refuse the query). Profile timeline, following lists, and HQ media metadata all use the authenticated web v1 API now; do not reintroduce hardcoded `/graphql/query` calls.

## Known pitfalls

**Browser response bodies are shadowdriver-specific** - Keep Instagram scraping based on DOM/Relay URLs, generic network events, and browser-context `fetch_bytes()` for images. Do not add raw-CDP assumptions to Instagram code beyond what shadowdriver's generic surface exposes.

**Large image fetches cross the protocol as base64** - Keep the browser message size limit high enough for real Instagram images.

**CDN shard mismatch** - DOM `img.src` and network requests can use different CDN subdomains. Compare by filename when matching variants.

**CDN domain diversity** - Instagram serves images from both `*.cdninstagram.com` and `*.fbcdn.net`. Use structural checks such as `_IG_CDN_RE`.

**Single-photo resolution** - `og:image`, JSON-LD image values, and some DOM `srcset` values can be 640px feed thumbnails. Prefer Relay script URLs and captured higher-quality variants. If no usable high-quality URL exists, fail cleanly.

**Video candidate noise** - Browser performance entries can include audio-only MP4s, alternate encodes, DASH segments, and recommendation videos. Collapse video posts to one best non-audio full MP4.

**HQ videos require authenticated metadata** - The best variants often appear only in `video_dash_manifest` from the authenticated web v1 media-info response; the profile-timeline feed and following-list v1 endpoints are separate and unaffected.

**Scan stubs are not completed posts** - A `post` row with NULL `post_type`/`scraped_at` or missing media files is pending work. Use `is_done()` rather than row existence, and pass the correct saved scope when checking profile membership.

**Saved posts share media but not membership** - Do not merge saved rows into normal profile rows. Keep queries scoped by `profile_post.saved` whenever reading, counting, deleting, retrying, or marking scanned membership.

**Generated-site video bugs need artifact checks** - A 200 response for an MP4 loaded through `<img>` still renders blank. The page must emit `<video>`, and the file must use a browser-playable codec.

## Debugging approach

When a scrape produces wrong results, isolate the failure locally, inspect output, fix it, rerun the focused case, and remove temporary diagnostics before finishing.

Start with diagnostics:

```
uv run tool ig doctor
uv run tool ig fingerprint
uv run tool ig fingerprint --live
```

For profile failures, verify scan and download separately:

```
uv run tool ig scan --live <username>
uv run tool ig scan --saved --live <username>
uv run tool ig get --live <username>
uv run tool ig get --saved --live <username>
sqlite3 instagram.db "SELECT pp.username, pp.saved, pp.shortid, p.post_type, p.scraped_at FROM profile_post pp LEFT JOIN post p ON p.shortid = pp.shortid WHERE pp.username='<username>' ORDER BY pp.saved, pp.seq LIMIT 20"
sqlite3 instagram.db "SELECT COUNT(*) FROM profile_post pp LEFT JOIN post p ON p.shortid = pp.shortid WHERE pp.username='<username>' AND pp.saved=0 AND (p.scraped_at IS NULL OR p.post_type IS NULL)"
sqlite3 instagram.db "SELECT COUNT(*) FROM profile_post pp LEFT JOIN post p ON p.shortid = pp.shortid WHERE pp.username='<username>' AND pp.saved=1 AND (p.scraped_at IS NULL OR p.post_type IS NULL)"
```

For generated-site video issues:

```
ffprobe -v error \
  -show_entries stream=index,codec_type,codec_name,width,height,duration \
  -show_entries format=duration \
  -of default=nw=1 site/images/<shortid>.mp4
rg -n -C 2 '<shortid>|<video|<img src="images/<shortid>' site/<username>.html site/data/<username>.json
```

Before finishing code changes, run:

```
make fmtlint
uv run pytest tests/ig -q
```
