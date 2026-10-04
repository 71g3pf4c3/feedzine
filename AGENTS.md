# AGENTS.md

Single-file Python CLI (no package, no pyproject): all logic lives in `bin/feedzine.py` (~1000 lines). `bin/eink.css` is the EPUB stylesheet, installed alongside the script. Runtime deps (all from the Nix flake, never pip): `typer` (CLI), `rich` (output/tables/status), `textual` (TUI), `httpx` (HTTP), `pydantic` (config validation), `Pillow` (images), plus external `pandoc` (HTML→Markdown and EPUB assembly).

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
- Runtime paths: config `~/.config/feedzine/config.toml`, state `state.json` in `workdir` (per-issue counter + seen guids + `pending` digest queue + `last_emit`), image cache `<workdir>/img/<preset>/` keyed by sha1 of URL. `issue` is idempotent: nothing new → no EPUB, no state change.
- Digests (`period = week|month`): articles accumulate in `state.pending` (feed settings snapshotted, dates as ISO strings — keep it JSON-serializable; `_pending_prepare` converts back to datetime/namespace on emit and must not mutate the pending entries in place). Emit happens on period boundary (`period_ready`) or `--force`; pending is cleared only after a successful build.
- Output formats are `epub | html | md` (`Config.format`, `--format` flag, `RunOpts.format` overrides config). Cover is EPUB-only; cover size comes from the preset (`PRESETS[*].cover`), so small-screen presets get small covers. `build_output()` is the single pandoc entrypoint; `squeeze_epub()` repacks with zip level 9 and must keep `mimetype` first and stored (OCF). `prune_issues()` filters by the format's extension.
- State is durable side-effectful data — tests must use `tmp_path`, never the real `~/.config`/`~/.cache`.

## Code conventions

- Comments, log strings, and error messages are in Russian; README and config example are Russian. Keep new user-facing strings Russian.
- `log()` prints through rich with `markup=False` — feed names may contain `[brackets]`; don't re-enable markup for user data.
- `http_get` retries only `httpx.TransportError` (2 extra attempts, 2s/4s backoff); HTTP status errors fail fast — covered by tests that monkeypatch `fz._client` and `time.sleep`. Full-text extraction is explicitly best-effort (Habr kek/v2 API, `<article>` fallback).
- The TUI (`FeedzineApp`) is tested headless via textual's `run_test()` pilot — monkeypatch `feed_stats` for network-free tests; long work runs in `run_worker(thread=True)` and must touch UI only via `call_from_thread`.
- Cover font resolution: `FEEDZINE_FONT` env → `<script dir>/fonts/cover.ttf` (installed from `dejavu_fonts` by the flake; devShell exports `FEEDZINE_FONT`). Fallback: Pillow `load_default`.
- `post_issue` runs with `shell=True` on purpose (user's own config); `%f` → EPUB path, `FEEDZINE_EPUB` env, 600s timeout.
