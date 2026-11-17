#!/usr/bin/env python3
"""Rename the project so a fork does not collide with upstream.

Replaces the package name, import paths, CLI command, environment-variable prefix
and documentation strings, then renames the package directory.

Usage::

    python scripts/rename_project.py myagent            # interactive
    python scripts/rename_project.py myagent --yes      # no prompt
    python scripts/rename_project.py myagent --dry-run  # show what would change
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

OLD_SLUG = "anyaclaw"
OLD_CAMEL = "Anyaclaw"
OLD_UPPER = "ANYACLAW"

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "build",
    "dist",
    "data",
    "demo-data",
    "demo-workspace",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "node_modules",
    ".anyaclaw",
}

TEXT_SUFFIXES = {
    ".py", ".md", ".toml", ".json", ".yml", ".yaml", ".txt", ".cfg", ".ini",
    ".html", ".css", ".js", ".sh", ".bat", ".ps1", ".example", ".gitignore",
}
TEXT_NAMES = {"Dockerfile", "Makefile", ".gitignore", ".dockerignore", ".env.example"}
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".docx", ".xlsx",
    ".pptx", ".zip", ".gz", ".db", ".sqlite", ".whl", ".pyc",
}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def camelize(slug: str) -> str:
    return "".join(part.capitalize() for part in slug.split("_") if part)


def is_text(path: Path) -> bool:
    if path.name in TEXT_NAMES:
        return True
    if path.suffix.lower() in BINARY_SUFFIXES:
        return False
    return path.suffix.lower() in TEXT_SUFFIXES


def iter_files(root: Path):
    for path in root.rglob("*"):
        parts = path.relative_to(root).parts
        if any(part in SKIP_DIRS or part.startswith("pytest-") for part in parts):
            continue
        if path.is_file():
            yield path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rename Anyaclaw for your own fork")
    parser.add_argument("name", help="new project name, e.g. myagent or My Agent")
    parser.add_argument("--root", default=".", help="repository root (default: .)")
    parser.add_argument("--dry-run", action="store_true", help="report only")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not (root / "pyproject.toml").is_file():
        print(f"error: {root} does not look like the project root (no pyproject.toml)", file=sys.stderr)
        return 2

    slug = slugify(args.name)
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", slug):
        print("error: name must start with a letter and contain only letters/digits", file=sys.stderr)
        return 2
    camel = camelize(slug)
    upper = slug.upper()

    replacements = [
        (OLD_CAMEL, camel),
        (OLD_UPPER, upper),
        (OLD_SLUG, slug),
    ]

    print(f"rename: {OLD_SLUG} -> {slug}   (package {camel}, env prefix {upper}_)")

    changed: list[Path] = []
    for path in iter_files(root):
        if not is_text(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        updated = text
        for old, new in replacements:
            updated = updated.replace(old, new)
        if updated != text:
            changed.append(path)
            if not args.dry_run:
                path.write_text(updated, encoding="utf-8")

    renames: list[tuple[Path, Path]] = []
    for path in sorted(iter_files(root), key=lambda p: len(p.parts), reverse=True):
        if OLD_SLUG not in path.name:
            continue
        target = path.with_name(path.name.replace(OLD_SLUG, slug))
        renames.append((path, target))

    directories = [
        path
        for path in root.rglob("*")
        if path.is_dir()
        and OLD_SLUG in path.name
        and not any(
            part in SKIP_DIRS or part.startswith("pytest-")
            for part in path.relative_to(root).parts
        )
    ]
    directories.sort(key=lambda p: len(p.parts), reverse=True)

    print(f"files to rewrite: {len(changed)}")
    for path in changed[:15]:
        print(f"  ~ {path.relative_to(root)}")
    if len(changed) > 15:
        print(f"  … and {len(changed) - 15} more")

    print(f"paths to rename: {len(renames) + len(directories)}")
    for path, target in renames:
        print(f"  {path.relative_to(root)} -> {target.name}")
    for directory in directories:
        print(f"  {directory.relative_to(root)}/ -> {directory.name.replace(OLD_SLUG, slug)}/")

    if args.dry_run:
        print("\ndry run: nothing changed")
        return 0

    if not args.yes:
        answer = input("\napply these changes? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("aborted")
            return 1

    for path, target in renames:
        if target.exists():
            continue
        path.rename(target)
    for directory in directories:
        target = directory.with_name(directory.name.replace(OLD_SLUG, slug))
        if not target.exists():
            shutil.move(str(directory), str(target))

    print(
        "\ndone. Next steps:\n"
        f"  - review README.md and set your repository URL\n"
        f"  - pip install -e '.[dev,web]'  (package is now {slug})\n"
        f"  - {slug} doctor\n"
        "  - update LICENSE copyright holder if you wish"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
