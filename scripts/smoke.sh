#!/bin/sh
# smoke.sh — download the release asset selected by one registry recipe,
# extract it, and verify the binary runs (`--version` prints anything).
#
# Usage: scripts/smoke.sh <recipe-file-or-repo>   e.g.
#        scripts/smoke.sh recipes/jqlang/jq.json
#        scripts/smoke.sh jqlang/jq
#
# Environment: GH_TOKEN or GITHUB_TOKEN is used for the GitHub API when set
# (optional; raises rate limits). Requires: curl, python3, tar (and unzip for
# .zip assets). POSIX sh only — runs on Debian, Alpine/busybox, etc.

set -eu

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

if [ "$#" -ne 1 ]; then
    echo "usage: $0 <recipe-file-or-repo>" >&2
    exit 2
fi

ARG=$1
case "$ARG" in
    recipes/*/*.json)
        RECIPE_PATH=$REPO_ROOT/$ARG ;;
    */*)
        RECIPE_PATH=$REPO_ROOT/recipes/$ARG.json ;;
    *)
        echo "error: argument must be a recipe path or org/repo: $ARG" >&2
        exit 2 ;;
esac

if [ ! -f "$RECIPE_PATH" ]; then
    echo "error: recipe not found: $RECIPE_PATH" >&2
    exit 2
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# --- Read the recipe -------------------------------------------------------
python3 - "$RECIPE_PATH" >"$WORK/recipe.env" <<'PYEOF'
import json, shlex, sys

with open(sys.argv[1], encoding="utf-8") as handle:
    recipe = json.load(handle)

selector = recipe.get("asset_selector") or {}
print("REPOSITORY=" + shlex.quote(recipe["repository"]))
print("SELECTOR_TYPE=" + shlex.quote(selector.get("type", "")))
print("SELECTOR_PATTERN=" + shlex.quote(selector.get("pattern", "")))
print("ARCHIVE_BINARY=" + shlex.quote(recipe.get("archive_binary", "")))
PYEOF
# shellcheck disable=SC1091
. "$WORK/recipe.env"

if [ -z "$SELECTOR_TYPE" ] || [ -z "$SELECTOR_PATTERN" ]; then
    echo "error: recipe has no usable asset_selector" >&2
    exit 2
fi

# --- Host platform tokens (uname -m/-s mapped to release-naming tokens) ----
HOST_ARCH=$(uname -m | tr '[:upper:]' '[:lower:]')
HOST_OS=$(uname -s | tr '[:upper:]' '[:lower:]')
case "$HOST_ARCH" in
    x86_64|amd64) ARCH_TOKENS="x86_64 amd64 x64 64bit" ;;
    aarch64|arm64) ARCH_TOKENS="aarch64 arm64" ;;
    armv7*|armhf|armv6*) ARCH_TOKENS="armv7 armhf armv6 arm" ;;
    i386|i686) ARCH_TOKENS="i386 i686 386" ;;
    *) ARCH_TOKENS="$HOST_ARCH" ;;
esac
case "$HOST_OS" in
    linux*) OS_TOKENS="linux unknown-linux" ;;
    darwin*) OS_TOKENS="darwin macos apple-darwin osx" ;;
    *) OS_TOKENS="$HOST_OS" ;;
esac

# --- Query the latest release ---------------------------------------------
AUTH_HEADER=""
if [ -n "${GH_TOKEN:-}" ]; then
    AUTH_HEADER="Authorization: Bearer $GH_TOKEN"
elif [ -n "${GITHUB_TOKEN:-}" ]; then
    AUTH_HEADER="Authorization: Bearer $GITHUB_TOKEN"
fi

API_URL="https://api.github.com/repos/$REPOSITORY/releases/latest"
if [ -n "$AUTH_HEADER" ]; then
    curl -fsSL -H "$AUTH_HEADER" -H "Accept: application/vnd.github+json" \
        "$API_URL" >"$WORK/release.json"
else
    curl -fsSL -H "Accept: application/vnd.github+json" "$API_URL" \
        >"$WORK/release.json"
fi

# --- Select the asset -------------------------------------------------------
ASSET_NAME=$(python3 - "$WORK/release.json" "$SELECTOR_TYPE" "$SELECTOR_PATTERN" \
    "$ARCH_TOKENS" "$OS_TOKENS" >"$WORK/asset.env" <<'PYEOF'
import json, re, shlex, sys

release = json.load(open(sys.argv[1], encoding="utf-8"))
sel_type, pattern = sys.argv[2], sys.argv[3]
arch_tokens = sys.argv[4].split()
os_tokens = sys.argv[5].split()


def matches(name):
    if sel_type == "substring":
        return pattern.lower() in name.lower()
    # Python re is a proxy for the RE2 dialect used on the Go side.
    return re.search(pattern, name) is not None


names = [a["name"] for a in release.get("assets", []) if matches(a["name"])]
if not names:
    print(f"error: selector matched no assets of {release.get('tag_name')}",
          file=sys.stderr)
    sys.exit(1)

# Prefer assets that name the host architecture, then the host OS; fall back
# to the lexicographically first match.


def rank(name):
    lowered = name.lower()
    arch_hit = min(
        (i for i, token in enumerate(arch_tokens) if token in lowered),
        default=len(arch_tokens),
    )
    os_hit = min(
        (i for i, token in enumerate(os_tokens) if token in lowered),
        default=len(os_tokens),
    )
    return (arch_hit, os_hit, name)


names.sort(key=rank)
print("ASSET_NAME=" + shlex.quote(names[0]))
PYEOF
) || exit 1
# shellcheck disable=SC1091
. "$WORK/asset.env"

ASSET_URL=$(python3 - "$WORK/release.json" "$ASSET_NAME" <<'PYEOF'
import json, sys

release = json.load(open(sys.argv[1], encoding="utf-8"))
for asset in release.get("assets", []):
    if asset["name"] == sys.argv[2]:
        print(asset["browser_download_url"])
        break
PYEOF
)

echo "smoke: $REPOSITORY @ $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tag_name"])' "$WORK/release.json") -> $ASSET_NAME"

# --- Download ---------------------------------------------------------------
curl -fsSL "$ASSET_URL" -o "$WORK/$ASSET_NAME"

# --- Extract / locate -------------------------------------------------------
DEST=$WORK/out
mkdir -p "$DEST"
BINARY=""
case "$ASSET_NAME" in
    *.tar.gz|*.tgz) tar -xzf "$WORK/$ASSET_NAME" -C "$DEST" ;;
    *.tar.xz) tar -xJf "$WORK/$ASSET_NAME" -C "$DEST" ;;
    *.tar.bz2) tar -xjf "$WORK/$ASSET_NAME" -C "$DEST" ;;
    *.zip) unzip -q "$WORK/$ASSET_NAME" -d "$DEST" ;;
    *)
        cp "$WORK/$ASSET_NAME" "$DEST/$(basename "$ASSET_NAME")"
        chmod +x "$DEST/$(basename "$ASSET_NAME")" 2>/dev/null || true
        ;;
esac

# Locate the binary, mirroring zenget's selectArchiveCandidate: exact archive
# path when archive_binary has a slash, then exact basename, then unique
# basename prefix, then the single executable file.
BINARY=$(python3 - "$DEST" "$ARCHIVE_BINARY" "$ASSET_NAME" <<'PYEOF'
import os, posixpath, sys

dest, archive_binary, asset_name = sys.argv[1], sys.argv[2], sys.argv[3]

candidates = []
for root, _dirs, files in os.walk(dest):
    for name in files:
        full = os.path.join(root, name)
        rel = os.path.relpath(full, dest).replace(os.sep, "/")
        if os.access(full, os.X_OK) and not os.path.islink(full):
            candidates.append(rel)

if archive_binary:
    clean = posixpath.normpath(archive_binary.replace("\\", "/"))
    if "/" in clean:
        exact = [c for c in candidates if posixpath.normpath(c) == clean]
        if len(exact) == 1:
            print(exact[0])
            sys.exit(0)
    base = posixpath.basename(clean)
    exact = [c for c in candidates if posixpath.basename(c) == base]
    if len(exact) == 1:
        print(exact[0])
        sys.exit(0)
    prefix = [c for c in candidates if posixpath.basename(c).startswith(base)]
    if len(prefix) == 1:
        print(prefix[0])
        sys.exit(0)

if len(candidates) == 1:
    print(candidates[0])
    sys.exit(0)

print(f"error: cannot locate binary in {asset_name!r}; "
      f"archive_binary={archive_binary!r}, executables: {candidates}",
      file=sys.stderr)
sys.exit(1)
PYEOF
)

chmod +x "$DEST/$BINARY" 2>/dev/null || true

# --- Run --------------------------------------------------------------------
echo "smoke: running $BINARY --version"
if ! "$DEST/$BINARY" --version >"$WORK/version.out" 2>&1; then
    : # tolerate a non-zero exit code when the binary still printed something
fi
cat "$WORK/version.out"

if [ ! -s "$WORK/version.out" ]; then
    echo "smoke: FAIL ($REPOSITORY): no version output" >&2
    exit 1
fi

echo "smoke: OK ($REPOSITORY)"
