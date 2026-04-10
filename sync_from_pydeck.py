#!/usr/bin/env python3
"""Sync themes from a local pydeck source tree into this themes repo.

Usage
-----
    python sync_from_pydeck.py [options]

Options
-------
    --pydeck-source PATH   Override the saved/auto-detected source path
    --dry-run              Show what would happen; make no changes
    --no-diff              Suppress the coloured per-file diff (shown by default)
    --no-generate          Skip running generate_manifest.py at the end
    --yes                  Skip confirmation prompts (non-interactive)
    --regen-conf           Re-prompt for the source path and save it again

Config
------
The confirmed source path is stored at:

    ~/.config/pydeck/pydeck-themes/path.json

On first run (or after --regen-conf) the script auto-detects and asks you
to confirm the path, then saves it so subsequent runs require no input.

Workflow
--------
For every theme directory found in the pydeck source tree:

  1. If the slug does **not** exist in this repo → treat as a brand-new theme
     and copy all its files into themes/<slug>/<version>/.

  2. If the slug **does** exist in the repo, compare the source files against
     the latest version folder (byte-for-byte, ignoring repo-only files and
     Python cache directories).

     - Files are identical → skip.
     - Files differ but the manifest version is the same as the repo's latest
       → bump the patch segment (e.g. 1.0.0 → 1.0.1), update manifest.json
       in the pydeck source, then copy all files into a new version folder.
     - Files differ and the manifest already has a higher version → copy into
       a new version folder using that version as-is.

After all themes have been processed, generate_manifest.py is run so the
root manifest.json stays current.
"""

from __future__ import annotations

import argparse
import difflib
import filecmp
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ── ANSI colours ───────────────────────────────────────────────────────────────

_RESET  = "\033[0m"
_RED    = "\033[31m"
_GREEN  = "\033[32m"
_CYAN   = "\033[36m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"

# ── Repo layout ────────────────────────────────────────────────────────────────

REPO_ROOT  = Path(__file__).resolve().parent
THEMES_DIR = REPO_ROOT / "themes"

# ── Config file ────────────────────────────────────────────────────────────────

CONFIG_PATH = Path.home() / ".config" / "pydeck" / "pydeck-themes" / "path.json"

# ── Files that live only in the marketplace repo ───────────────────────────────

REPO_ONLY_FILES: frozenset[str] = frozenset({
    "catalog.json",
    "icon.svg",
    "icon.png",
    "license.txt",
    "lisence.txt",
    "LICENSE",
    "LICENSE.txt",
    "LICENSE.md",
})

EXCLUDE_DIRS: frozenset[str] = frozenset({"__pycache__"})
EXCLUDE_SUFFIXES: frozenset[str] = frozenset({".pyc", ".pyo"})

# ── Candidate pydeck source paths (auto-detection order) ──────────────────────

_CANDIDATES: list[Path] = [
    Path.home() / "Documents" / "GitHub" / "pydeck" / "themes",
    REPO_ROOT.parents[0] / "pydeck" / "themes",
    Path.home() / "pydeck" / "themes",
]

# ── Config helpers ─────────────────────────────────────────────────────────────

def _load_config() -> Optional[Path]:
    """Return the saved source path from the config file, or None."""
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text())
        raw = data.get("pydeck_source", "")
        if not raw:
            return None
        p = Path(raw).expanduser().resolve()
        return p if p.is_dir() else None
    except (json.JSONDecodeError, OSError):
        return None


def _save_config(source: Path) -> None:
    """Persist the chosen source path to the config file."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps({"pydeck_source": str(source)}, indent=2) + "\n"
    )

# ── Semver helpers ─────────────────────────────────────────────────────────────

def _semver_tuple(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in version.split("."))
    except ValueError:
        return (0,)


def _bump_patch(version: str) -> str:
    """Increment the last numeric segment: '1.0.0' → '1.0.1'."""
    parts = version.split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except (ValueError, IndexError):
        parts.append("1")
    return ".".join(parts)


def _is_version_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    parts = path.name.split(".")
    return len(parts) >= 2 and all(p.isdigit() for p in parts)

# ── Source/repo helpers ────────────────────────────────────────────────────────

def _source_files(theme_dir: Path) -> dict[str, Path]:
    """Return {relative_path: absolute_path} for all copyable source files."""
    result: dict[str, Path] = {}
    for p in theme_dir.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix in EXCLUDE_SUFFIXES:
            continue
        rel = p.relative_to(theme_dir)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        result[str(rel)] = p
    return result


def _is_version_dir_nonempty(path: Path) -> bool:
    """Return True if path is a non-empty semver-named directory."""
    return _is_version_dir(path) and any(path.rglob("*"))


def _latest_version_dir(slug_dir: Path) -> Optional[Path]:
    """Return the highest semver-named subdirectory that is non-empty, or None."""
    version_dirs = [d for d in slug_dir.iterdir() if _is_version_dir_nonempty(d)]
    if not version_dirs:
        return None
    return max(version_dirs, key=lambda d: _semver_tuple(d.name))


def _read_version(manifest_path: Path) -> str:
    """Read the 'version' field from a manifest.json, or return '0.0.0'."""
    try:
        data = json.loads(manifest_path.read_text())
        return str(data.get("version", "1.0.0"))
    except (json.JSONDecodeError, OSError):
        return "1.0.0"


def _write_version(manifest_path: Path, new_version: str) -> None:
    """Update the 'version' field in a manifest.json file in-place.

    Uses a targeted string replacement so the rest of the JSON formatting
    is preserved exactly, preventing spurious diffs on the next sync run.
    """
    text = manifest_path.read_text()
    data = json.loads(text)
    old_version = str(data.get("version", ""))
    updated = text.replace(
        f'"version": "{old_version}"',
        f'"version": "{new_version}"',
        1,
    )
    if updated == text:
        data["version"] = new_version
        updated = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    manifest_path.write_text(updated)

# ── Comparison ─────────────────────────────────────────────────────────────────

def _json_equal(a: Path, b: Path) -> bool:
    """Return True if two JSON files are semantically identical."""
    try:
        return json.loads(a.read_text()) == json.loads(b.read_text())
    except (json.JSONDecodeError, OSError):
        return False


def _files_changed(source_files: dict[str, Path], repo_version_dir: Path) -> bool:
    """Return True if any source file differs from the installed version."""
    for rel, src_path in source_files.items():
        if Path(rel).name in REPO_ONLY_FILES:
            continue
        repo_path = repo_version_dir / rel
        if not repo_path.exists():
            return True
        if src_path.suffix == ".json":
            if not _json_equal(src_path, repo_path):
                return True
        elif not filecmp.cmp(str(src_path), str(repo_path), shallow=False):
            return True

    for repo_path in repo_version_dir.rglob("*"):
        if not repo_path.is_file():
            continue
        if repo_path.name in REPO_ONLY_FILES:
            continue
        rel = str(repo_path.relative_to(repo_version_dir))
        if any(part in EXCLUDE_DIRS for part in Path(rel).parts):
            continue
        if rel not in source_files:
            return True

    return False

# ── Diff display ───────────────────────────────────────────────────────────────

def _is_binary(path: Path) -> bool:
    """Heuristic: file is binary if it contains a null byte in the first 8 KB."""
    try:
        return b"\x00" in path.read_bytes()[:8192]
    except OSError:
        return False


def _print_file_diff(rel: str, src: Path | None, repo: Path | None) -> None:
    """Print a coloured unified diff for one file."""
    label_a = f"a/{rel}"
    label_b = f"b/{rel}"

    if src is None:
        line_count = len(repo.read_bytes().splitlines()) if repo else 0
        print(f"{_BOLD}{_RED}deleted file: {rel}  ({line_count} lines){_RESET}")
        return

    if repo is None:
        line_count = len(src.read_bytes().splitlines())
        print(f"{_BOLD}{_GREEN}new file: {rel}  ({line_count} lines){_RESET}")
        return

    if _is_binary(src) or _is_binary(repo):
        print(f"{_BOLD}binary file changed: {rel}{_RESET}")
        return

    src_lines  = src.read_text(errors="replace").splitlines(keepends=True)
    repo_lines = repo.read_text(errors="replace").splitlines(keepends=True)
    chunks = list(difflib.unified_diff(repo_lines, src_lines, fromfile=label_a, tofile=label_b))
    if not chunks:
        return
    for line in chunks:
        line = line.rstrip("\n")
        if line.startswith("---") or line.startswith("+++"):
            print(f"{_BOLD}{line}{_RESET}")
        elif line.startswith("@@"):
            print(f"{_CYAN}{line}{_RESET}")
        elif line.startswith("+"):
            print(f"{_GREEN}{line}{_RESET}")
        elif line.startswith("-"):
            print(f"{_RED}{line}{_RESET}")
        else:
            print(f"{_DIM}{line}{_RESET}")


def _print_theme_diff(
    slug: str,
    source_files: dict[str, Path],
    repo_version_dir: Path,
) -> None:
    """Print a coloured diff between source and the latest repo version."""
    print(f"\n{_BOLD}diff  {slug}  ({repo_version_dir.name} → new){_RESET}")

    printed_any = False

    for rel, src_path in sorted(source_files.items()):
        if Path(rel).name in REPO_ONLY_FILES:
            continue
        repo_path = repo_version_dir / rel
        if not repo_path.exists():
            _print_file_diff(rel, src_path, None)
            printed_any = True
        elif src_path.suffix == ".json":
            if not _json_equal(src_path, repo_path):
                _print_file_diff(rel, src_path, repo_path)
                printed_any = True
        elif not filecmp.cmp(str(src_path), str(repo_path), shallow=False):
            _print_file_diff(rel, src_path, repo_path)
            printed_any = True

    for repo_path in sorted(repo_version_dir.rglob("*")):
        if not repo_path.is_file():
            continue
        if repo_path.name in REPO_ONLY_FILES:
            continue
        rel = str(repo_path.relative_to(repo_version_dir))
        if any(part in EXCLUDE_DIRS for part in Path(rel).parts):
            continue
        if rel not in source_files:
            _print_file_diff(rel, None, repo_path)
            printed_any = True

    if not printed_any:
        print(f"  {_DIM}(no textual differences to display){_RESET}")

    print()


# ── Copy logic ─────────────────────────────────────────────────────────────────

def _copy_theme_to_repo(
    source_files: dict[str, Path],
    dest_version_dir: Path,
    dry_run: bool,
) -> None:
    """Copy source files into dest_version_dir (create it if needed)."""
    if dry_run:
        return
    dest_version_dir.mkdir(parents=True, exist_ok=True)
    for rel, src_path in source_files.items():
        dest = dest_version_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src_path), str(dest))

# ── Path resolution ────────────────────────────────────────────────────────────

def _detect_candidate() -> Optional[Path]:
    for candidate in _CANDIDATES:
        if candidate.is_dir():
            return candidate
    return None


def _prompt_for_path(detected: Optional[Path], yes: bool) -> Path:
    """Ask the user to confirm or provide the source path, then return it."""
    if detected:
        if yes:
            print(f"Using pydeck source: {detected}")
            return detected
        print(f"\nDetected pydeck themes source path:")
        print(f"  {detected}")
        answer = input("Is this correct? [Y/n] ").strip().lower()
        if answer in ("", "y", "yes"):
            return detected

    if not detected:
        print("Could not auto-detect the pydeck themes/ directory.")

    while True:
        raw = input("Enter the full path to pydeck's themes/ directory: ").strip()
        p = Path(raw).expanduser().resolve()
        if p.is_dir():
            return p
        print(f"  Path not found: {p}. Try again.")


def _resolve_source(
    override: Optional[str],
    regen_conf: bool,
    yes: bool,
) -> Path:
    """Return the confirmed source path, loading/saving config as needed."""

    if override:
        p = Path(override).expanduser().resolve()
        if not p.is_dir():
            sys.exit(f"Error: --pydeck-source path does not exist: {p}")
        return p

    if regen_conf:
        source = _prompt_for_path(_detect_candidate(), yes)
        _save_config(source)
        print(f"Path saved to {CONFIG_PATH}")
        return source

    saved = _load_config()
    if saved is not None:
        print(f"Using saved pydeck source: {saved}")
        print(f"  (run with --regen-conf to change)")
        return saved

    source = _prompt_for_path(_detect_candidate(), yes)
    _save_config(source)
    print(f"Path saved to {CONFIG_PATH}")
    return source

# ── Catalog helper ─────────────────────────────────────────────────────────────

def _ensure_catalog(
    slug_dir: Path,
    src_manifest: Path,
    dry_run: bool,
) -> None:
    """Create a stub catalog.json for a new theme if one doesn't exist yet."""
    catalog_path = slug_dir / "catalog.json"
    if catalog_path.exists() or dry_run:
        return
    meta: dict = {}
    if src_manifest.exists():
        try:
            meta = json.loads(src_manifest.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    catalog = {
        "summary": meta.get("description", ""),
        "author": "PyDeck Team",
    }
    catalog_path.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False) + "\n"
    )

# ── Per-theme sync ─────────────────────────────────────────────────────────────

def _sync_theme(
    slug: str,
    source_theme_dir: Path,
    dry_run: bool,
    show_diff: bool = False,
) -> str:
    """Sync one theme. Returns a human-readable status line."""
    source_files = _source_files(source_theme_dir)
    if not source_files:
        return f"  SKIP {slug}: no files found in source"

    slug_dir = THEMES_DIR / slug
    tag = "[DRY RUN] " if dry_run else ""

    # ── Brand-new theme ───────────────────────────────────────────────────────
    if not slug_dir.exists() or _latest_version_dir(slug_dir) is None:
        src_manifest = source_theme_dir / "manifest.json"
        version = _read_version(src_manifest) if src_manifest.exists() else "1.0.0"
        dest_version_dir = slug_dir / version
        _copy_theme_to_repo(source_files, dest_version_dir, dry_run)
        _ensure_catalog(slug_dir, src_manifest, dry_run)
        return f"  {tag}NEW     {slug}  →  {dest_version_dir.relative_to(REPO_ROOT)}"

    # ── Existing theme ────────────────────────────────────────────────────────
    latest_dir = _latest_version_dir(slug_dir)  # type: ignore[arg-type]

    if not _files_changed(source_files, latest_dir):
        return f"  SKIP    {slug}  (unchanged, latest={latest_dir.name})"

    if show_diff:
        _print_theme_diff(slug, source_files, latest_dir)

    src_manifest = source_theme_dir / "manifest.json"
    src_version  = _read_version(src_manifest) if src_manifest.exists() else latest_dir.name
    repo_version = latest_dir.name

    if _semver_tuple(src_version) <= _semver_tuple(repo_version):
        new_version = _bump_patch(repo_version)
        if not dry_run:
            _write_version(src_manifest, new_version)
        bump_note = f" (bumped {repo_version} → {new_version})"
    else:
        new_version = src_version
        bump_note = f" (source version {src_version} > repo {repo_version})"

    dest_version_dir = slug_dir / new_version
    _copy_theme_to_repo(source_files, dest_version_dir, dry_run)
    return (
        f"  {tag}UPDATE  {slug}  →  "
        f"{dest_version_dir.relative_to(REPO_ROOT)}{bump_note}"
    )

# ── Main ───────────────────────────────────────────────────────────────────────

def sync_all(source_root: Path, dry_run: bool, show_diff: bool = False) -> None:
    slug_dirs = sorted(
        [d for d in source_root.iterdir() if d.is_dir() and not d.name.startswith(".")],
        key=lambda d: d.name.lower(),
    )
    if not slug_dirs:
        print("No theme directories found in source.", file=sys.stderr)
        return

    print(f"Scanning {len(slug_dirs)} source theme(s)…\n")
    for slug_dir in slug_dirs:
        print(_sync_theme(slug_dir.name, slug_dir, dry_run, show_diff=show_diff))


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(
        description="Sync themes from a local pydeck tree into this themes repo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--pydeck-source",
        metavar="PATH",
        help="Path to pydeck's themes/ directory (overrides saved config)",
    )
    parser.add_argument(
        "--regen-conf",
        action="store_true",
        help=f"Re-prompt for the source path and update {CONFIG_PATH}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without writing any files",
    )
    parser.add_argument(
        "--no-generate",
        action="store_true",
        help="Skip running generate_manifest.py after syncing",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Accept the auto-detected/saved path without prompting",
    )
    parser.add_argument(
        "--no-diff",
        action="store_true",
        help="Suppress the coloured per-file diff (shown by default)",
    )
    args = parser.parse_args()

    source = _resolve_source(
        override=args.pydeck_source,
        regen_conf=args.regen_conf,
        yes=args.yes,
    )

    print(f"\nSource : {source}")
    print(f"Repo   : {REPO_ROOT}")
    if args.dry_run:
        print("Mode   : DRY RUN (no files will be written)\n")
    else:
        print()

    sync_all(source, dry_run=args.dry_run, show_diff=not args.no_diff)

    if not args.no_generate:
        print("\nRunning generate_manifest.py…")
        cmd = [sys.executable, str(REPO_ROOT / "generate_manifest.py")]
        if args.dry_run:
            cmd.append("--dry-run")
        result = subprocess.run(cmd, cwd=str(REPO_ROOT))
        sys.exit(result.returncode)


if __name__ == "__main__":
    main()
