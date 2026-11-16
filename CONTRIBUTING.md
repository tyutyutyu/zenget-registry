# Contributing recipes

## Adding a recipe

1. Create `recipes/<owner>/<repo>.json`. The path is derived from the
   `repository` field and vice versa — `scripts/validate.py` rejects any
   mismatch, so there is exactly one recipe file per repository.
2. Run `python3 scripts/validate.py` (or `task dev:verify`) from the repository root. It re-implements
   the strict Go decoder contract (required fields, unknown-field rejection,
   null rejection for optional fields, platform enum + sorted + unique check,
   selector/checksum shape, `archive_binary`/`target_name` constraints,
   canonical 2-space-indent formatting) and also validates `registry.json`.
3. Update the index entry in `registry.json` with
   `sha256sum recipes/<owner>/<repo>.json` and keep the entries sorted by
   `(repository, path)`; `validate.py` fails otherwise.
4. Run `sh scripts/smoke.sh <owner>/<repo>` locally (Linux amd64, needs
   `curl` + `python3`; `tar`/`unzip` for archives). The PR gate re-runs it on
   debian/glibc, alpine/musl, and arm64/QEMU.

## Authoring rules

- **Verify against the real release.** Query
  `curl -s https://api.github.com/repos/<owner>/<repo>/releases/latest` and
  check the actual asset names and archive layout. Do not guess
  `archive_binary`: download the archive and list its contents. Many archives
  nest the binary in a versioned top directory — see the `sharkdp/fd` recipe,
  where `archive_binary: "fd"` still resolves because zenget falls back to an
  exact basename match.
- **Prefer statically linked or musl builds.** A musl/static binary runs on
  both glibc and musl hosts, so one recipe covers more of the smoke matrix.
- **List only platforms you can defend** in `supported_systems`: the platform
  enum is fixed, entries must be lowercase, unique, and sorted, and the smoke
  matrix only executes linux/amd64 and linux/arm64.
- **Selectors:** `substring` is a case-insensitive literal match; `regex`
  uses Go's RE2 dialect. Anchor regexes (`^...$`) so they cannot drift onto
  neighbouring assets (e.g. `yq_linux_amd64` vs `yq_linux_amd64.tar.gz`).
- **RE2 caveat:** `scripts/validate.py` compiles regexes with Python `re`,
  which accepts constructs RE2 rejects (lookaround, backreferences). The Go
  decoder is the authoritative gate; keep patterns simple (alternation,
  classes, anchors) so both accept them.
- **Checksums are optional; never invent them.** Include `checksum` only when
  the release actually publishes a checksum asset you have inspected:
  - `jqlang/jq` publishes `sha256sum.txt` → `format: sha256sum`.
  - `junegunn/fzf` publishes `fzf_<version>_checksums.txt` → `format: sha256sum`.
  - `mikefarah/yq` publishes a `checksums` bundle in a non-sha256sum layout,
    and `sharkdp/fd` publishes no checksum file at all → both recipes omit
    `checksum`.
- Recipe files must stay canonical: 2-space indent, struct field order
  (`schema_version`, `repository`, `supported_systems`, `asset_selector`,
  `archive_binary`, `target_name`, `checksum`), trailing newline.
  `validate.py` enforces the exact bytes.
