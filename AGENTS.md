# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A **static theme marketplace catalog** for PyDeck. It contains no application code — only
theme assets (CSS + JSON metadata) plus two dependency-free Python 3 scripts that build and
populate the catalog. There is no build system, no test suite, no linter config, and no
third-party dependencies (stdlib only).

Themes are *authored upstream* in the PyDeck app's themes directory
(`$XDG_DATA_HOME/pydeck/themes`, default `~/.local/share/pydeck/themes`) and pulled into
this repo by `sync_from_pydeck.py`. Editing theme CSS directly in this repo will be
overwritten on the next sync unless the same edit is made upstream.

## Commands

```bash
# Pull themes from the local PyDeck install, version-bump changed ones, then regenerate manifest.json
python sync_from_pydeck.py                 # shows a coloured diff and prompts on first run
python sync_from_pydeck.py --dry-run       # preview only, writes nothing
python sync_from_pydeck.py --yes --no-diff # non-interactive
python sync_from_pydeck.py --pydeck-source ~/some/themes   # override source
python sync_from_pydeck.py --regen-conf    # re-prompt and re-save the source path

# Rebuild the root manifest only (also run automatically at the end of a sync)
python generate_manifest.py
python generate_manifest.py --dry-run
python generate_manifest.py --label "Official · Stable"
```

The confirmed source path is cached at `~/.config/pydeck/pydeck-themes/path.json`.

## Manifest hierarchy

Three JSON layers, each with a distinct owner. Getting these confused is the main hazard:

| File | Owner | Contents |
|---|---|---|
| `manifest.json` (root) | **generated — never hand-edit** | catalog index: `schema_version`, `generated_at`, `label`, `themes[]` |
| `themes/<slug>/catalog.json` | hand-maintained, repo-only | `summary`, `author`, optional `licenses` |
| `themes/<slug>/<version>/manifest.json` | **synced from upstream** | `label`, `description`, and either `variants: {dark, light}` or `scheme: "dark"\|"light"` for single-file themes |

Field precedence when `generate_manifest.py` builds a root entry:

- `name` ← version manifest `label` → previous root entry → slug
- `summary` ← `catalog.json` → previous root entry → version manifest `description`
- `author`, `licenses` ← `catalog.json` → previous root entry (default author `"Unknown"`)
- `icon_path` ← `icon.svg` preferred over `icon.png` in `themes/<slug>/`, else previous entry

The **fallback to the previous root manifest is load-bearing**: regenerating without a
`catalog.json` present would otherwise silently drop `author`/`summary`/`licenses`. Delete or
truncate `manifest.json` and that data is gone.

## Versioning model

Version directories are semver-named (`themes/<slug>/1.0.0/`) and treated as immutable
releases — a change never edits an existing version folder, it creates a new one. The
highest semver directory becomes `latest`.

**Never edit a file inside an existing version directory.** Doing so silently breaks
already-installed clients: they recorded that version in `.marketplace.json` and only re-fetch
when `latest` changes, so they keep the old bytes forever. The sync cannot protect you here —
it compares the source against the repo **working tree**, not against git HEAD, so a hand-edit
already sitting in a version directory reads as "unchanged" and reports SKIP. If `git status`
shows a modified file under `themes/<slug>/<version>/`, that is a mistake to undo, not a
change to commit: restore the file and let the sync create a new version instead.

`sync_from_pydeck.py` decides per theme:

- slug absent (or has no non-empty version dir) → **NEW**: copy into the source manifest's
  version, and stub out a `catalog.json` (`summary` from `description`, author `"PyDeck Team"`).
- files byte-identical to the latest version dir → **SKIP** (`.json` files compare
  semantically, not byte-wise; `REPO_ONLY_FILES` such as `catalog.json`/`icon.*`/licenses are
  excluded from comparison in both directions).
- files differ, source version ≤ repo latest → **UPDATE**: patch-bump, write the new version
  back into the *upstream source* `manifest.json`, then copy into the new version dir.
- files differ, source version > repo latest → copy into that version as-is.

Note the side effect: a sync mutates the upstream PyDeck source's `manifest.json` version
field (via a targeted string replace, to preserve formatting and avoid spurious future diffs).
`--dry-run` suppresses this.

Three exclusion sets guard the comparison — if a sync ever reports every theme as changed,
suspect a new file the installer writes that none of these cover:

- `EXCLUDE_FILES` — `.marketplace.json`, the install-tracking stamp PyDeck writes into every
  installed theme (`{"slug", "version"}`). Filtered out of `_source_files` so it is never
  compared, copied, or diffed. Left unfiltered it makes every theme look modified and
  triggers a chain of empty patch bumps.
- `EXCLUDE_SLUGS` — `default`, PyDeck's built-in appearance. It ships with the app and lives
  in the local themes directory like any other theme, so without the skip-list every sync
  re-adds it (it was removed in commit a41680b).
- `REPO_ONLY_FILES` — `catalog.json`, `icon.*`, licence files: these exist only here and must
  not count as "missing from source".

## Theme CSS contract

Each variant CSS file defines the PyDeck design tokens on `:root`. All 37 variant files
define the same core set, so a new theme should define all of them:

`--bg-0` `--bg-1` `--bg-2` `--bg-3` `--surface` `--surface-hover` `--border` `--border-focus`
`--text-0` `--text-1` `--text-2` `--accent` `--accent-glow` `--success` `--error`
`--ha-on` `--ha-off` (Home Assistant on/off state colours)

Optional: `--radius`, `--radius-sm`, `--font`. Themes may additionally override component
selectors beyond `:root` (the `2000s` theme is a full ~1400-line chrome restyle; most others
are ~20-line palette swaps).

Dual-variant themes ship `dark.css` + `light.css` with a `variants` map; single-variant themes
ship `theme.css` with a `scheme` field.

## Branches

`canary` is the working branch; `stable` is the default/PR target. The catalog `label`
defaults to `"Official · Canary"` — pass `--label` when generating for the stable channel.
