# zenget recipe registry

Central, CI-tested recipe registry for [zenget](../zenget). A *recipe* is a
strict JSON document that tells zenget how to pick, verify, and install the
single binary of a GitHub release. The authoritative contract is the strict Go
decoder in the zenget repository
(`internal/recipe/recipe.go`); the JSON Schema mirror lives at
[`schemas/recipe-v1.schema.json`](schemas/recipe-v1.schema.json) and is copied
verbatim from the zenget repository.

## Layout

```text
registry.json                     canonical index over all recipes
registry-state.json               last smoke-tested upstream tag per recipe
schemas/recipe-v1.schema.json     JSON Schema mirror of the recipe contract
recipes/<owner>/<repo>.json       one recipe per repository
scripts/validate.py               stdlib-only recipe + index validator
scripts/smoke.sh                  downloads the selected asset and runs the binary
.github/workflows/                validate / smoke / update pipelines
```

`registry.json` entries pin the exact bytes of each recipe via a lowercase
SHA-256 digest, so the index is only consistent when the recipe files are
byte-identical to what was reviewed.

## Trust model

- The registry is **CI-tested but not provenance-attested**. The smoke matrix
  proves that each recipe currently resolves a real release asset and that the
  extracted binary actually runs on the tested platforms. It proves nothing
  about build provenance, upstream account compromise, or malicious releases.
- zenget keeps its own security boundaries when consuming recipes: checksum
  policy (TASK-0025) and the policy gates still apply, and a recipe is a
  selection rule, not a provenance proof.
- Consumed state is **commit-SHA pinned**: consumers point zenget at an exact
  immutable commit of this registry, never a moving branch or tag.
- This repository is a **local pilot**. Per
  [decision-0002](../zenget/.backlog/decisions/), the current state is
  **NO-GO for production launch**: an owner-reviewed operating model, a
  two-person review process (Registry Steward + Security/Release Reviewer), a
  license review, and a budget decision are required before this registry can
  become a supported supply-chain component. Do not treat the CI status as a
  product promise until those gates are met.

## How zenget consumes this registry

```sh
# Allow the recipe for one repository through the policy gate.
zenget policy recipe allow github.com/<owner>/<repo>

# Register the registry source pinned to a full 40-hex commit SHA.
zenget registry add default github:<owner>/<repo>@<full-40-hex-commit>:

# Fetch the pinned snapshot and refresh local state.
zenget registry sync default
```

Unknown repositories fall back to zenget's built-in heuristics (platform /
libc / archive-format rules), so a missing recipe is a degradation, not a
failure. Registry recipes override heuristics only for the repositories that
are explicitly covered.

## Contribution workflow

1. Fork, add `recipes/<owner>/<repo>.json` (see
   [CONTRIBUTING.md](CONTRIBUTING.md) for authoring rules), and run
   `python3 scripts/validate.py` locally.
2. Recompute the index digest:
   `sha256sum recipes/<owner>/<repo>.json` and update `registry.json`
   (validated canonically by `scripts/validate.py`, which fails on drift).
3. Open a PR. The PR runs `validate` on every push and the `smoke` matrix
   (debian glibc amd64, alpine musl amd64, debian glibc arm64 via QEMU) on
   every recipe the PR touches.
4. Merge requires the smoke matrix to be green.

## Smoke matrix and its limits

| Leg | Platform | Native? |
|-----|----------|---------|
| debian:stable-slim | linux/amd64, glibc | yes |
| alpine:latest | linux/amd64, musl (busybox tar) | yes |
| debian:stable-slim via `docker run --platform linux/arm64` | linux/arm64, glibc | no — QEMU emulated |

Limits:

- **QEMU arm64 is weaker than native.** It verifies correctness, not
  performance, and shares the host kernel; treat it as a smoke signal, not a
  platform guarantee.
- **No native macOS or Windows leg** exists in the matrix. Recipes list such
  platforms only when their asset names and archive layout make the selector
  unambiguous.
- Recipes should prefer **statically linked or musl builds** so the same
  recipe works on both glibc and musl hosts (e.g. the `sharkdp/fd` recipe
  selects the `*-unknown-linux-musl` tarball, which also runs on glibc).

## Scheduled updater

`.github/workflows/update.yml` runs daily (and on demand): it compares each
indexed repository's latest upstream release tag against
`registry-state.json`, re-runs the shared smoke matrix via `workflow_call`
for the changed or never-tested recipes, and then opens a PR refreshing the
tested tags. **Pilot caveat:** this repository currently has no GitHub remote,
so the final `gh pr create` step is committed but cannot be exercised here; it
requires a real remote plus the branch protection / ruleset model from
decision-0002.

## Workflow security policy

- Third-party actions are limited to `actions/checkout@v4`,
  `docker/setup-qemu-action@v3`, and `docker/setup-buildx-action@v3`
  (documented exceptions; everything else is bash, python3 stdlib, and the
  docker CLI). Any future third-party action must be pinned to a full commit
  SHA and justified in the PR.
- Workflows run with the `GITHUB_TOKEN` restricted to `contents: read` by
  default (the updater's PR job elevates `contents: write` and
  `pull-requests: write` explicitly, in its own job).
- The GitHub API is called with `curl`/urllib and `GITHUB_TOKEN`; no
  third-party API clients, no personal access tokens, **no repository
  secrets** are used or stored.

## License

MIT, see [LICENSE](LICENSE). Recipes describe upstream projects owned by their
respective authors; each upstream tool keeps its own license.
