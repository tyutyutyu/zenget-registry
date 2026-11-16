#!/usr/bin/env python3
"""Validate every recipe and the registry index against the zenget contracts.

This is a stdlib-only re-implementation of the strict Go decoders in the zenget
repository:

- internal/recipe/recipe.go    (recipe schema, strict fields, canonical form)
- internal/registry/registry.go (registry.json index: unique, canonically
  sorted by (repository, path), sha256 digests over recipe file bytes)

Note on regex dialects: recipe regex fields use Go's RE2 syntax on the zenget
side. Python's `re` module accepts a broader dialect, so a pattern that passes
here is only a proxy: the Go decoder is the authoritative RE2 gate and rejects
lookarounds/backreferences that Python would accept.

Usage: scripts/validate.py [repo-root]   (defaults to the parent of scripts/)
Exit status: 0 when everything is valid, 1 with diagnostics on stderr/stdout.
"""

import hashlib
import json
import posixpath
import re
import sys
from pathlib import Path

SCHEMA_VERSION = 1

REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

PLATFORMS = {
    "linux/386", "linux/amd64", "linux/arm", "linux/arm64",
    "darwin/amd64", "darwin/arm64",
    "windows/386", "windows/amd64", "windows/arm", "windows/arm64",
}

RECIPE_FIELDS = {
    "$schema", "schema_version", "repository", "supported_systems",
    "asset_selector", "archive_binary", "target_name", "checksum",
}
SELECTOR_FIELDS = {"version", "type", "pattern"}
CHECKSUM_FIELDS = {"type", "pattern", "format"}

# Canonical JSON rendering: 2-space indent, struct field order as emitted by
# zenget's recipe.Marshal / registry.MarshalIndex, trailing newline.
RECIPE_KEY_ORDER = [
    "$schema", "schema_version", "repository", "supported_systems",
    "asset_selector", "archive_binary", "target_name", "checksum",
]
INDEX_KEY_ORDER = ["schema_version", "entries"]
ENTRY_KEY_ORDER = ["repository", "path", "sha256"]


class ValidationError(Exception):
    pass


def fail(errors, location, message):
    errors.append(f"{location}: {message}")


def reject_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValidationError(f"duplicate JSON key {key!r}")
        seen[key] = value
    return seen


def load_strict(location, raw):
    """Parse one JSON value and reject trailing data, like the Go decoder."""
    text = raw.decode("utf-8")
    try:
        return json.loads(text, object_pairs_hook=reject_duplicates)
    except ValidationError as exc:
        raise ValidationError(f"{location}: {exc}")
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{location}: invalid JSON: {exc}")


def _render_value(value, depth, order):
    """Render value at the given nesting depth (2 spaces per level), matching
    Go's json.MarshalIndent(r, "", "  ") plus the struct field order used by
    zenget's recipe.Marshal."""
    if isinstance(value, dict):
        if not value:
            return "{}"
        keys = [k for k in order if k in value] + [k for k in value if k not in order]
        pad = "  " * (depth + 1)
        lines = [pad + json.dumps(key, ensure_ascii=False) + ": "
                 + _render_value(value[key], depth + 1, order)
                 for key in keys]
        return "{\n" + ",\n".join(lines) + "\n" + "  " * depth + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        pad = "  " * (depth + 1)
        lines = [pad + _render_value(item, depth + 1, order) for item in value]
        return "[\n" + ",\n".join(lines) + "\n" + "  " * depth + "]"
    return json.dumps(value, ensure_ascii=False)


def canonical_bytes(value):
    return (_render_value(value, 0, RECIPE_KEY_ORDER) + "\n").encode("utf-8")


def canonical_index_bytes(value):
    entries = value["entries"]
    lines = ['{', '  "schema_version": 1,', '  "entries": [']
    for i, entry in enumerate(entries):
        lines.append("    {")
        lines.append(f'      "repository": {json.dumps(entry["repository"], ensure_ascii=False)},')
        lines.append(f'      "path": {json.dumps(entry["path"], ensure_ascii=False)},')
        lines.append(f'      "sha256": {json.dumps(entry["sha256"], ensure_ascii=False)}')
        lines.append("    }" + ("," if i < len(entries) - 1 else ""))
    lines.append("  ]")
    lines.append("}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def clean_relative_archive_path(value):
    """Mirror of cleanRelativeArchivePath in internal/recipe/recipe.go."""
    if not value or "\x00" in value or "\\" in value or posixpath.isabs(value):
        return None
    clean = posixpath.normpath(value)
    if clean in (".", "..") or clean.startswith("../"):
        return None
    if any(part == ".." for part in clean.split("/")):
        return None
    return clean


def valid_target_name(name):
    return (
        name not in ("", ".", "..")
        and "/" not in name and "\\" not in name and "\x00" not in name
    )


def validate_recipe(path, root, errors):
    location = path.relative_to(root).as_posix()
    raw = path.read_bytes()
    try:
        recipe = load_strict(location, raw)
    except ValidationError as exc:
        fail(errors, location, str(exc))
        return None

    if not isinstance(recipe, dict):
        fail(errors, location, "recipe must be a JSON object")
        return None

    for key in recipe:
        if key not in RECIPE_FIELDS:
            fail(errors, location, f"unknown field {key!r}")
    for key in ("supported_systems", "asset_selector", "archive_binary",
                "target_name", "checksum"):
        if recipe.get(key) is None and key in recipe:
            fail(errors, location, f'field "{key}" must not be null')
    if "schema_version" not in recipe:
        fail(errors, location, "missing schema_version")
    elif recipe["schema_version"] != SCHEMA_VERSION:
        fail(errors, location,
             f"unsupported schema_version {recipe['schema_version']!r}; want {SCHEMA_VERSION}")
    if "repository" not in recipe:
        fail(errors, location, "missing repository")
        return None

    repository = recipe["repository"]
    if not isinstance(repository, str) or not REPOSITORY_RE.match(repository):
        fail(errors, location, f"invalid repository {repository!r}: expected org/repo")

    # Path <-> repository <-> filename consistency.
    expected_path = f"recipes/{repository}.json"
    if location != expected_path:
        fail(errors, location,
             f"path must be recipes/<owner>/<repo>.json derived from repository "
             f"(expected {expected_path})")

    systems = recipe.get("supported_systems")
    if systems is not None:
        if not isinstance(systems, list):
            fail(errors, location, "supported_systems must be an array")
        else:
            if not systems:
                fail(errors, location, "supported_systems must not be empty")
            cleaned = [s.lower().strip() if isinstance(s, str) else "" for s in systems]
            for original, canonical in zip(systems, cleaned):
                if not isinstance(original, str) or not canonical:
                    fail(errors, location, "supported_systems contains an empty platform")
                    continue
                if canonical not in PLATFORMS:
                    fail(errors, location,
                         f"supported_systems contains unsupported platform {original!r}")
            if len(set(cleaned)) != len(cleaned):
                fail(errors, location, "supported_systems contains duplicates")
            if systems != sorted(systems):
                fail(errors, location, "supported_systems must be sorted canonically")

    selector = recipe.get("asset_selector")
    if selector is not None:
        if not isinstance(selector, dict):
            fail(errors, location, "asset_selector must be an object")
        else:
            for key in selector:
                if key not in SELECTOR_FIELDS:
                    fail(errors, location, f"asset_selector has unknown field {key!r}")
            if selector.get("version") != SCHEMA_VERSION:
                fail(errors, location,
                     f"asset_selector version must be {SCHEMA_VERSION}")
            sel_type = selector.get("type")
            if sel_type not in ("substring", "regex"):
                fail(errors, location,
                     f"unsupported asset_selector type {sel_type!r}")
            pattern = selector.get("pattern")
            if not isinstance(pattern, str) or pattern == "":
                fail(errors, location, "asset_selector pattern must not be empty")
            elif sel_type == "regex":
                try:
                    re.compile(pattern)
                except re.error as exc:
                    fail(errors, location,
                         f"invalid asset_selector regex (Python proxy for RE2): {exc}")

    archive_binary = recipe.get("archive_binary")
    if archive_binary is not None:
        if not isinstance(archive_binary, str):
            fail(errors, location, "archive_binary must be a string")
        elif clean_relative_archive_path(archive_binary) is None:
            fail(errors, location, f"invalid archive_binary {archive_binary!r}")

    target_name = recipe.get("target_name")
    if target_name is not None:
        if not isinstance(target_name, str) or not valid_target_name(target_name):
            fail(errors, location, f"invalid target_name {target_name!r}")

    checksum = recipe.get("checksum")
    if checksum is not None:
        if not isinstance(checksum, dict):
            fail(errors, location, "checksum must be an object")
        else:
            for key in checksum:
                if key not in CHECKSUM_FIELDS:
                    fail(errors, location, f"checksum has unknown field {key!r}")
            if checksum.get("type") not in ("exact", "regex"):
                fail(errors, location,
                     f"unsupported checksum type {checksum.get('type')!r}")
            pattern = checksum.get("pattern")
            if (not isinstance(pattern, str) or not pattern.strip()
                    or "\x00" in pattern):
                fail(errors, location, "checksum pattern must not be empty")
            elif checksum.get("type") == "regex":
                try:
                    re.compile(pattern)
                except re.error as exc:
                    fail(errors, location,
                         f"invalid checksum regex (Python proxy for RE2): {exc}")
            if checksum.get("format") not in ("raw", "sha256sum"):
                fail(errors, location,
                     f"unsupported checksum format {checksum.get('format')!r}")

    canonical = canonical_bytes(recipe)
    if raw != canonical:
        fail(errors, location,
             "recipe file is not canonically formatted (2-space indent, "
             "struct field order, trailing newline)")
    return repository


def validate_index(root, errors, recipes_on_disk):
    index_path = root / "registry.json"
    location = "registry.json"
    raw = index_path.read_bytes()
    try:
        index = load_strict(location, raw)
    except ValidationError as exc:
        fail(errors, location, str(exc))
        return

    if not isinstance(index, dict):
        fail(errors, location, "registry index must be a JSON object")
        return
    for key in index:
        if key not in ("schema_version", "entries"):
            fail(errors, location, f"unknown field {key!r}")
    if index.get("schema_version") != SCHEMA_VERSION:
        fail(errors, location,
             f"unsupported schema_version {index.get('schema_version')!r}; want {SCHEMA_VERSION}")
    entries = index.get("entries")
    if not isinstance(entries, list):
        fail(errors, location, '"entries" must be an array')
        return

    seen_repos = set()
    seen_paths = set()
    normalized = []
    for position, entry in enumerate(entries):
        loc = f"registry.json entries[{position}]"
        if not isinstance(entry, dict):
            fail(errors, loc, "entry must be an object")
            continue
        for key in entry:
            if key not in ("repository", "path", "sha256"):
                fail(errors, loc, f"unknown field {key!r}")
        repository = entry.get("repository")
        path = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(repository, str) or not REPOSITORY_RE.match(repository):
            fail(errors, loc, f"invalid repository {repository!r}")
            continue
        if repository in seen_repos:
            fail(errors, loc, f"duplicate repository {repository!r}")
        seen_repos.add(repository)
        expected = f"recipes/{repository}.json"
        if path != expected:
            fail(errors, loc,
                 f"path {path!r} must equal {expected!r} derived from the repository")
        elif path in seen_paths:
            fail(errors, loc, f"duplicate path {path!r}")
        seen_paths.add(path)
        if not isinstance(digest, str) or not DIGEST_RE.match(digest):
            fail(errors, loc,
                 f"invalid sha256 digest {digest!r}: expected lowercase 64 hex characters")
            continue
        recipe_file = root / path
        if not recipe_file.is_file():
            fail(errors, loc, f"recipe file {path} is missing on disk")
            continue
        actual = hashlib.sha256(recipe_file.read_bytes()).hexdigest()
        if actual != digest:
            fail(errors, loc,
                 f"sha256 mismatch for {path}: index has {digest}, file has {actual}")
        normalized.append({"repository": repository, "path": path, "sha256": digest})

    normalized_sorted = sorted(normalized,
                               key=lambda e: (e["repository"], e["path"]))
    if normalized_sorted != normalized:
        fail(errors, location, "entries are not canonically sorted by (repository, path)")

    if raw != canonical_index_bytes(index):
        fail(errors, location,
             "registry.json is not canonically formatted (2-space indent, trailing newline)")

    indexed_repos = {e["repository"] for e in normalized}
    for repo in sorted(recipes_on_disk - indexed_repos):
        fail(errors, location,
             f"recipe {repo} exists on disk but is missing from the index (orphan)")
    for repo in sorted(indexed_repos - recipes_on_disk):
        fail(errors, location,
             f"index entry {repo} has no recipe file under recipes/")


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    root = root.resolve()
    errors = []

    recipe_files = sorted((root / "recipes").glob("*/*.json"))
    if not recipe_files:
        fail(errors, "recipes/", "no recipe files found")
    recipes_on_disk = set()
    for path in recipe_files:
        repository = validate_recipe(path, root, errors)
        if repository:
            if repository in recipes_on_disk:
                fail(errors, path.relative_to(root).as_posix(),
                     f"duplicate recipe for repository {repository!r}")
            recipes_on_disk.add(repository)

    if (root / "registry.json").is_file():
        validate_index(root, errors, recipes_on_disk)
    else:
        fail(errors, "registry.json", "index file is missing")

    if errors:
        print(f"validation FAILED ({len(errors)} error(s)):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"validation OK: {len(recipe_files)} recipe(s), "
          f"{len(recipes_on_disk)} unique repository(ies), registry index consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
