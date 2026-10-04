# AGENTS.md

Single-file Python CLI (no package, no pyproject): all logic lives in `bin/feedzine.py` (~1000 lines). `bin/eink.css` is the EPUB stylesheet, installed alongside the script. Runtime deps (all from the Nix flake, never pip): `typer` (CLI), `rich` (output/tables/status), `textual` (TUI), `httpx` (HTTP), `pydantic` (config validation), `Pillow` (images), `fontTools` (cover glyph coverage), plus external `pandoc` (HTML→Markdown and EPUB assembly).

## Commands

```bash
nix develop                    # dev shell: python env + pytest + pandoc, FEEDZINE_FONT exported
pytest tests -q                # unit tests (no network, no pandoc needed)
pytest tests/test_core.py::test_parse_rss   # single test
nix flake check -L             # what CI runs: unit tests + build + HM-module eval
nix run . -- --help            # smoke: run CLI from the flake
```

CI (`.github/workflows/ci.yml`) runs only `nix flake check -L` plus a CLI smoke (`--help`, `init --config <tmp>`). There is no lint/typecheck step.

## Repo quirks

- Tests import the app via `importlib` file path (`bin/feedzine.py`), not a package — `bin/` has no `__init__.py` and none should be added.
- Version lives only in `flake.nix` (`version = "..."`); bump it there on release. There is no `__version__` in the script.
- The flake source is the git tree: new files must be `git add`-ed before `nix flake check` / `nix build` can see them.
- `homeManagerModules.feedzine` lives in `nix/hm-module.nix` (takes the flake as arg for the default package). Its `settings` option maps 1:1 to fields of `Config` in `bin/feedzine.py` — keep both in sync when adding config keys; `formats.toml` renders list-of-attrsets `feed` as `[[feed]]`.
- The httpx client is created lazily (`_get_client`): constructing it at module import crashes inside `nix flake check`'s sandbox where `SSL_CERT_FILE` points to a missing path. Keep it lazy.
- Config is validated by pydantic with `extra="forbid"` — unknown keys raise `ValidationError` (this is a feature: catches typos). CLI run-time flags live in `RunOpts`, never in `Config`.
- Runtime paths: config `~/.config/feedzine/config.toml`, state `state.json` in `workdir`, image cache `<workdir>/img/<preset>/` keyed by sha1 of URL. `issue` is idempotent: nothing new → no EPUB, no state change.
- Journals: feeds with `journal = "<id>"` build into separate journals — own title (`[journal.<id>]`, main = `cfg.title`), own out dir (`out/<id>/`), own period/keep_issues, own issue counter, own pending and last_emit. `seen` is one shared pool. State shape: `{"seen": {}, "issue": {jid: n}, "pending": {jid: [...]}, "last_emit": {jid: date}}`; old flat state (issue int / pending list / last_emit str) migrates to journal `main` in `load_state`. One journal's build failure must not touch the others (`_emit_journal` isolated, rc=1).
- Digests (`period = day|week|month`, per-journal override): articles accumulate in `state.pending[jid]` (feed settings snapshotted, dates as ISO strings — keep it JSON-serializable; `_pending_prepare` converts back to datetime/namespace on emit and must not mutate the pending entries in place). Emit happens on period boundary (`period_ready`) or `--force`; pending is cleared only after a successful build.
- Backfill (`feedzine backfill --weeks N`): weekly retro-issues from what feeds currently serve (seen ignored, window cut by article date). Weeks emit oldest→newest via `_emit_journal` with `force=True`, dated by the week's latest article. The whole window (emitted + cap-cut) is marked seen so regular flow doesn't double-emit; pending is restored minus emitted. `--dry-run` must not touch the filesystem (makedirs happen after the dry-run return). An empty-after-cap bucket skips without consuming an issue number. Do not invent history sources beyond feed output.
- Full text: Habr via kek/v2; everything else via `_extract_page` (largest `<article>`/`<main>` by text volume, scripts/styles/nav stripped). Sniffer: if the item link yields no content (aggregators like HN), try the first external URL from the feed item description — exactly one attempt. `full_text` values: `auto` (Habr + stub-feeds whose summary is under 300 chars of text — the fetch decides automatically), `true`/`always` (generic), `false`.
- Junk gate: `article_md` returns None when the body has less than `min_article_chars` (global default 120, per-feed override, 0 = off) prose chars AND no downloaded images (`_md_img_count == 0`) — link-stubs, paywall stubs and empty summaries never reach the EPUB, but photo-posts (short caption + working image) survive. Placeholder text `[картинка не скачалась: …]` is stripped by `_md_prose_len` and never counts as prose. `_emit_journal` skips None, and if a journal's every article gets filtered, no issue number is consumed, pending is cleared and last_emit advances. Feed snapshot in pending carries {name, full_text, section, min_article_chars}.
- Degradations are surfaced, never hidden: failed full-text fetches land in the `retry_fulltext` state pool (RETRY_MAX_TRIES=5, RETRY_MAX_DAYS=14; success → article re-emitted next issue as «Докатнуто · …» section via `_pending_add`, exhausted/stale → dropped); feed fetch failures land in `down_feeds` (url → {name, jid, error, since}, popped on recovery). `_emit_journal` collects `failures` from `article_md(..., failures=[])` (kind=fulltext with feed snapshot / kind=image with count) and renders the «Что не попало в выпуск» report section. Tracking pixels (medium `/_/stat`, `/collect?`, `analytics`, `pixel`, `beacon`) are silently dropped by `rewrite_images`; real image failures get an inline placeholder. `rewrite_images` returns a 3-tuple `(html, done, failed)`. `process_image` resolves relative URLs via `urljoin(base, url)` (base = article origin) and returns `(name|None, errcode|None)`.
- Covers (`cover_pattern`): generative monochrome art — `mandelbrot|julia|truchet|phyllotaxis|moire|tree|ridge|sierpinski`, seeded by crc32(title)^crc32(date)^(n*2654435761) — same issue re-renders identically, next issue differs. Text layer (title/№/date/sections) is sanitized against the font cmap via fontTools (`cover_text_safe`): glyphs missing from the font are dropped with a log line, never tofu. fonttools is a runtime dep from the flake.
- Output formats are `epub | html | md` (`Config.format`, `--format` flag, `RunOpts.format` overrides config). Cover is EPUB-only; cover size comes from the preset (`PRESETS[*].cover`), so small-screen presets get small covers. `build_output()` is the single pandoc entrypoint; `squeeze_epub()` repacks with zip level 9 and must keep `mimetype` first and stored (OCF). `prune_issues()` filters by the format's extension.
- State is durable side-effectful data — tests must use `tmp_path`, never the real `~/.config`/`~/.cache`.

## Code conventions

- Comments, log strings, and error messages are in Russian; README and config example are Russian. Keep new user-facing strings Russian.
- `log()` prints through rich with `markup=False` — feed names may contain `[brackets]`; don't re-enable markup for user data.
- `http_get` retries only `httpx.TransportError` (2 extra attempts, 2s/4s backoff); HTTP status errors fail fast — covered by tests that monkeypatch `fz._client` and `time.sleep`. Full-text extraction is explicitly best-effort (Habr kek/v2 API, `<article>` fallback).
- The TUI (`FeedzineApp`) is tested headless via textual's `run_test()` pilot — monkeypatch `feed_stats` and `run_issue` for network-free tests; long work runs in `run_worker(thread=True)` and must touch UI only via `call_from_thread`. `i` builds with `force=False` (periods respected), `I` forces, `d` dry-runs; journal status bar + RichLog output panel are part of the UI contract.
- The webui (`feedzine web`) is stdlib-only (`http.server`, ThreadingHTTPServer) — no new runtime deps. `make_web_server(cfg, opts, host, port)` returns the server without serving (used by tests with port 0); `run_web` adds serve_forever + browser opening. JSON API: `GET /api/overview` (journals/pending/feeds/issues), `GET/POST /api/build` (single-flight, 409 while running, POST body `{"force": bool}`), `POST /api/scan` (background feed_stats). It never mutates the config file (HM-managed); defaults to 127.0.0.1 because there is no auth. `log()` also appends to `LOG_RING` (deque) which webui/TUI use for tails.
- Cover font resolution: `FEEDZINE_FONT` env → `<script dir>/fonts/cover.ttf` (installed from `dejavu_fonts` by the flake; devShell exports `FEEDZINE_FONT`). Fallback: Pillow `load_default`.
- `post_issue` runs with `shell=True` on purpose (user's own config); `%f` → EPUB path, `FEEDZINE_EPUB` env, 600s timeout.
