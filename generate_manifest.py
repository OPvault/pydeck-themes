#!/usr/bin/env python3
"""Generate the root manifest.json for the pydeck-themes marketplace repo.

Usage
-----
    python generate_manifest.py [options]

Options
-------
    --label TEXT      Catalog label string (default: "Official · Canary")
    --output PATH     Output file path     (default: manifest.json)
    --dry-run         Print the result to stdout instead of writing it

Discovery logic
---------------
For each themes/<slug>/ directory:

  1. Version directories are any sub-folders whose name parses as a semver
     tuple (e.g. "1.0.0", "1.0.1").  They are sorted newest-first; the
     highest becomes `latest`.

  2. Per-version fields (label → name, description → summary) are read from
     the version's own manifest.json.

  3. Catalog-only fields (summary, author, licenses) are read from an optional
     themes/<slug>/catalog.json.  When that file is absent the script falls
     back to the matching entry in the existing root manifest.json so nothing
     is lost on regeneration.

  4. The icon path is auto-detected: icon.svg is preferred over icon.png.

Themes are written in alphabetical order by name.

Related tooling
---------------
``sync_from_pydeck.py`` copies from a local PyDeck themes directory into this
repo; auto-detection prefers **``$XDG_DATA_HOME/pydeck/themes``** (default
**``~/.local/share/pydeck/themes``**). Use ``default_pydeck_themes_install_dir()``
below when you need that path from Python.
"""

from __future__ import annotations

import argparse
import json
import re
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Repo layout ────────────────────────────────────────────────────────────────

REPO_ROOT     = Path(__file__).resolve().parent
THEMES_DIR    = REPO_ROOT / "themes"
ROOT_MANIFEST = REPO_ROOT / "manifest.json"


def default_pydeck_themes_install_dir() -> Path:
    """Return ``$XDG_DATA_HOME/pydeck/themes`` (default ``~/.local/share/pydeck/themes``)."""

    raw = (os.environ.get("XDG_DATA_HOME") or "").strip()
    base = Path(raw).expanduser().resolve() if raw else Path.home() / ".local" / "share"
    return base / "pydeck" / "themes"

SCHEMA_VERSION = 1
DEFAULT_LABEL  = "Official · Canary"
ICON_PRIORITY  = ("icon.svg", "icon.png")

# ── root_url ──────────────────────────────────────────────────────────────────
# The manifest is fetched from a vanity domain that proxies it, so consumers
# cannot derive where the files live from the URL they fetched. root_url states
# it outright: the base every icon_path / version path hangs off.

ROOT_URL_TEMPLATE = "https://raw.githubusercontent.com/{owner}/{repo}/{branch}/"


def _git_output(*args: str) -> str:
    import subprocess
    try:
        r = subprocess.run(["git", *args], cwd=REPO_ROOT,
                           capture_output=True, text=True)
    except OSError:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def default_root_url() -> str:
    """Raw base for the checked-out branch, or "" when it cannot be determined.

    Only a default: any script that writes a manifest for a branch other than
    the one it is standing on must pass --root-url explicitly.
    """

    remote = _git_output("remote", "get-url", "origin")
    branch = _git_output("rev-parse", "--abbrev-ref", "HEAD")
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", remote)
    if not m or not branch or branch == "HEAD":
        return ""
    return ROOT_URL_TEMPLATE.format(owner=m.group(1), repo=m.group(2), branch=branch)


# ── Semver helpers ─────────────────────────────────────────────────────────────

def _semver_tuple(version: str) -> Tuple[int, ...]:
    """Return a sortable tuple for a semver string, e.g. "1.0.1" → (1, 0, 1)."""
    try:
        return tuple(int(x) for x in version.split("."))
    except ValueError:
        return (0,)


def _is_version_dir(path: Path) -> bool:
    """True if *path* is a directory whose name looks like a semver string."""
    if not path.is_dir():
        return False
    parts = path.name.split(".")
    return len(parts) >= 2 and all(p.isdigit() for p in parts)


# ── Existing root manifest (for field fallbacks) ───────────────────────────────

def _load_existing_root() -> Dict[str, Dict[str, Any]]:
    """Return a slug → entry dict from the current root manifest, or {}."""
    if not ROOT_MANIFEST.exists():
        return {}
    try:
        data = json.loads(ROOT_MANIFEST.read_text())
        return {t["slug"]: t for t in data.get("themes", [])}
    except (json.JSONDecodeError, KeyError):
        return {}


# ── Per-theme discovery ────────────────────────────────────────────────────────

def _icon_path(slug_dir: Path, slug: str) -> Optional[str]:
    """Return the repo-relative icon path, or None if no icon exists."""
    for name in ICON_PRIORITY:
        if (slug_dir / name).exists():
            return f"themes/{slug_dir.name}/{name}"
    return None


def _catalog_meta(slug_dir: Path) -> Dict[str, Any]:
    """Read themes/<slug>/catalog.json if it exists, else return {}."""
    f = slug_dir / "catalog.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError as exc:
        print(f"  WARNING: {f} is invalid JSON — {exc}", file=sys.stderr)
        return {}


def _read_version_manifest(version_dir: Path) -> Optional[Dict[str, Any]]:
    """Read and return the parsed manifest.json inside a version directory."""
    f = version_dir / "manifest.json"
    if not f.exists():
        print(f"  WARNING: missing {f}", file=sys.stderr)
        return None
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError as exc:
        print(f"  WARNING: {f} is invalid JSON — {exc}", file=sys.stderr)
        return None


def _build_theme_entry(
    slug: str,
    slug_dir: Path,
    existing: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Build a root-manifest theme entry for *slug*, or None on failure."""

    # ── Collect and sort version directories ──────────────────────────────────
    version_dirs = sorted(
        [d for d in slug_dir.iterdir() if _is_version_dir(d)],
        key=lambda d: _semver_tuple(d.name),
    )
    if not version_dirs:
        print(f"  SKIP {slug}: no version directories found", file=sys.stderr)
        return None

    # ── Read all version manifests ────────────────────────────────────────────
    versions: List[Dict[str, Any]] = []
    latest_meta: Optional[Dict[str, Any]] = None

    for vdir in version_dirs:
        vmeta = _read_version_manifest(vdir)
        if vmeta is None:
            continue
        versions.append({
            "version": vdir.name,
            "path":    f"themes/{slug_dir.name}/{vdir.name}",
        })
        latest_meta = vmeta

    if not versions or latest_meta is None:
        print(f"  SKIP {slug}: no readable version manifests", file=sys.stderr)
        return None

    latest_version = versions[-1]["version"]

    # ── Resolve catalog-only fields ───────────────────────────────────────────
    # Priority: catalog.json > existing root manifest > sensible defaults
    catalog    = _catalog_meta(slug_dir)
    prev_entry = existing.get(slug, {})

    name    = latest_meta.get("label")   or prev_entry.get("name")    or slug
    summary = (catalog.get("summary")
               or prev_entry.get("summary")
               or latest_meta.get("description", ""))
    author  = catalog.get("author")     or prev_entry.get("author")  or "Unknown"

    icon = _icon_path(slug_dir, slug) or prev_entry.get("icon_path")
    if icon is None:
        print(f"  WARNING: {slug} has no icon file", file=sys.stderr)
        icon = ""

    licenses = catalog.get("licenses") or prev_entry.get("licenses") or []

    entry: Dict[str, Any] = {
        "name":      name,
        "slug":      slug,
        "summary":   summary,
        "author":    author,
        "latest":    latest_version,
        "icon_path": icon,
        "versions":  versions,
    }
    if licenses:
        entry["licenses"] = licenses
    return entry


# ── Main ───────────────────────────────────────────────────────────────────────

def generate(label: str, root_url: str, output: Path, dry_run: bool) -> None:
    existing = _load_existing_root()
    themes: List[Dict[str, Any]] = []

    slug_dirs = sorted(
        [d for d in THEMES_DIR.iterdir() if d.is_dir()],
        key=lambda d: d.name.lower(),
    )

    print(f"Scanning {len(slug_dirs)} theme director{'y' if len(slug_dirs) == 1 else 'ies'}…")

    for slug_dir in slug_dirs:
        slug = slug_dir.name
        entry = _build_theme_entry(slug, slug_dir, existing)
        if entry:
            themes.append(entry)
            versions_str = ", ".join(v["version"] for v in entry["versions"])
            print(f"  ✓ {entry['name']} ({slug})  [{versions_str}]  latest={entry['latest']}")

    themes.sort(key=lambda t: t["name"].lower())

    root = {
        "schema_version": SCHEMA_VERSION,
        "generated_at":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "label":          label,
        "root_url":       root_url,
        "themes":         themes,
    }

    output_text = json.dumps(root, indent=2, ensure_ascii=False) + "\n"

    if dry_run:
        print("\n── dry-run output ──────────────────────────────────────────────")
        print(output_text)
    else:
        output.write_text(output_text)
        print(f"\nWrote {len(themes)} theme(s) → {output.relative_to(REPO_ROOT)}")


def main() -> None:
    default_install = default_pydeck_themes_install_dir()
    parser = argparse.ArgumentParser(
        description=(
            "Generate the root manifest.json for the pydeck-themes repo. "
            f"PyDeck installs themes under {default_install} by default "
            "(override with $XDG_DATA_HOME); sync_from_pydeck.py copies from there."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--label",
        default=DEFAULT_LABEL,
        help=f'Catalog label (default: "{DEFAULT_LABEL}")',
    )
    parser.add_argument(
        "--root-url",
        default=default_root_url(),
        help="Base URL the entry paths resolve against "
             "(default: the raw URL of the checked-out branch)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT_MANIFEST,
        help=f"Output path (default: {ROOT_MANIFEST.name})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the generated JSON to stdout without writing any file",
    )
    args = parser.parse_args()
    generate(label=args.label, root_url=args.root_url,
             output=args.output, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
