# AGENTS.md

Single-file Python CLI (no package, no pyproject): all logic lives in `bin/feedzine.py` (~470 lines, Python stdlib + Pillow). `bin/eink.css` is the EPUB stylesheet, installed alongside the script. External runtime deps: `pandoc` (HTML→Markdown and EPUB assembly) and Pillow — both provided by the Nix flake, not by pip.

## Commands

```bash
nix develop                    # dev shell: python3 + pytest + pillow + pandoc
pytest tests -q                # unit tests (no network, no pandoc needed)
pytest tests/test_core.py::test_parse_rss   # single test
nix flake check -L             # what CI runs: unit tests + build
nix run . -- --help            # smoke: run CLI from the flake
```

CI (`.github/workflows/ci.yml`) runs only `nix flake check -L` plus a CLI smoke (`--help`, `init --config <tmp>`). There is no lint/typecheck step. `flake check` also builds `checks.hm-module` — a minimal Home Manager config importing the module, which validates module eval and the generated config.toml.

## Repo quirks

- Tests import the app via `importlib` file path (`bin/feedzine.py`), not a package — `bin/` has no `__init__.py` and none should be added.
- Version lives only in `flake.nix` (`version = "..."`); bump it there on release. There is no `__version__` in the script.
- The flake source is the git tree: new files must be `git add`-ed before `nix flake check` / `nix build` can see them.
- `homeManagerModules.feedzine` lives in `nix/hm-module.nix` (takes the flake as arg for the default package). Its `settings` option maps 1:1 to fields of `load_config` in `bin/feedzine.py` — keep both in sync when adding config keys; `formats.toml` renders list-of-attrsets `feed` as `[[feed]]`.
- Runtime paths: config `~/.config/feedzine/config.toml`, state `state.json` in `out` dir (per-issue counter + seen guids), image cache `~/.cache/feedzine/img/<preset>/` keyed by sha1 of URL. `issue` is idempotent: nothing new → no EPUB, no state change.
- State is durable side-effectful data — tests must use `tmp_path`, never the real `~/.config`/`~/.cache`.

## Code conventions

- Comments, log strings, and error messages are in Russian; README and config example are Russian. Keep new user-facing strings Russian.
- Config is TOML parsed with stdlib `tomllib` (no schema validation beyond defaults in `load_config`).
- Network fetches have no retry logic beyond `http_get` timeouts — keep it that way unless adding tests; full-text extraction is explicitly best-effort (Habr kek/v2 API, `<article>` fallback).
