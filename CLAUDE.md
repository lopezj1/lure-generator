# lure-generator

Config-driven pipeline: profile image + TOML spec → balanced lure CAD (build123d) → G-code for a Snapmaker U1 (OrcaSlicer CLI) or a Shapeoko 3 XXL (Carbide Motion, GRBL 1.1 fork). First family: two-section glide bait.

**The plan lives in GitHub issues, not in files.** The root epic is #1 and its sub-issue tree is the roadmap. Read the epic and the active feature before starting work. Don't recreate a PLAN.md.

## Architecture rules

- **`spec/` is data, `src/` is behavior.** Anything tuned between builds (dimensions, weights, densities, targets, machines, tools, feeds) is TOML/CSV/JSON in `spec/`. Python only knows how to build a lure family or drive a machine type.
  - `spec/designs/<family>/{family.toml, profiles/, variants/}`: a variant is a buildable config.
  - `spec/printers/<machine>/`, `spec/routers/<machine>/`: machine.toml, flattened slicer profiles or post.toml/tools/feeds, `reference/` sample G-code.
  - `spec/hardware/*.toml` (measured [min, max] masses, referenced by id), `spec/materials.toml`.
- **Config layering:** `spec/designs/_common.toml` ← `family.toml` ← variant ← CLI `--set a.b=c`. Every build writes `resolved.toml`.
- **Package `src/luregen/`:** `config/`, `profile/`, `families/` (one module per bait family behind a Protocol), `geometry/`, `physics/`, `dfm/`, `fab/{printer,router}/`, `report/`, `commands/`, `cli.py`.
- **CLI subcommands self-register:** each is a module in `src/luregen/commands/` exposing `register(app)`. Add a module and never edit `cli.py`. Import heavy libraries inside the command function.
- **Contracts:**
  - `luregen.profile.table.StationTable`: nose and tail tips plus interior stations `x, center_z, v_radius, half_width`, with a CSV v1 format. Produced by F1, consumed by F2.
  - `luregen.config.LureConfig`: a resolved config is self-contained (`resolved.toml` reloads without the catalogs).
- **Coordinates:** x from the nose tip (0) toward the tail, z up (0 = horizontal axis through the nose tip), y lateral with the body symmetric about y = 0. Lengths in mm, mass in g (reports also show oz), densities in g/cm³.
- **Outputs** go to `builds/<family>/<variant>/<YYYYMMDD-HHMMSS>-<gitsha>[-dirty]/` (gitignored), with a `latest` symlink. `lure fab` reads only a build folder.

## Commands

```
uv run lure --help
uv run lure build spec/designs/glide/variants/<v>.toml [--set key=value]
uv run lure fab builds/glide/<v>/latest --format print|cnc [--machine <name>]
uv run pytest
uv run ruff check && uv run ruff format
```
Python 3.13 via uv. System libs needed by the OCP wheel: `libgl1 libx11-6`. Slicing uses `xvfb-run` only for thumbnails.

## Issue hierarchy

- Labels: `type:epic` (only #1), `type:feature`, `type:task`; `area:{infra,profile,geometry,physics,dfm,print,cnc,field}`; `needs-input` (waiting on the owner: images, sample G-code, measured weights); `blocked`.
- Features are sub-issues of #1; tasks are sub-issues of a feature. The installed `gh` (2.23) has no sub-issue flags, so link with GraphQL:
  ```
  P=$(gh issue view <parent> --json id -q .id); C=$(gh issue view <child> --json id -q .id)
  gh api graphql -f query='mutation($p:ID!,$c:ID!){addSubIssue(input:{issueId:$p,subIssueId:$c}){issue{number}}}' -f p="$P" -f c="$C"
  ```
- **The issue body is the current truth. Comments are history.** When a decision changes scope or design, edit the body (and the epic's decisions list if it's cross-cutting), then mention the change in a comment.

## Branch / worktree / PR workflow

- Never commit to `main`. One branch + worktree per **feature**:
  ```
  git fetch origin
  git worktree add ../lure-generator.wt/<feature#>-<slug> -b feat/<feature#>-<slug> origin/main
  ```
- Commits reference the task: `profile: fit EFD contour (#14)`.
- When the feature is done, open one PR (template in `.github/`). It has `Closes #<feature>` and a `Closes #<task>` line per completed task, and gets squash-merged.
- After merge: `git worktree remove ../lure-generator.wt/<feature#>-<slug>` and delete the branch.

## Parallel work (subagents + worktrees)

- Features with no dependency between them run in parallel, one background agent per feature or feature part, each in its own worktree. Dependency waves:
  1. F0
  2. F1, F2, F6a (#31–#34), F7a (#36–#39)
  3. F3, F7b
  4. F4, then F5
  5. F6b, F7 end-to-end, F8
- The orchestrating session creates the worktrees (naming above) and gives each agent its absolute worktree path. Agents work only inside that path.
- **Don't touch shared files on a feature branch:**
  - `pyproject.toml`/`uv.lock`: all dependencies are pinned in F0. If one is truly missing, stop and report back instead of adding it.
  - `cli.py`: add a `commands/` module instead.
  - Another feature's modules.
  - The contracts above: change those only in their own feature's PR, and announce it on the epic.
- **A feature may land in several PRs** (e.g. F6a then F6b). Each PR closes only its own tasks, and the last one also closes the feature.
- Before opening a PR, rebase on `origin/main` and rerun ruff and pytest. The owner merges PRs; agents never merge.

## Session protocol

1. **Start:** identify the active feature/task (ask if unclear), then `gh issue view <n> --comments` for it and its parent feature.
2. **During:** keep work inside that feature's worktree. If something affects another feature, comment on that issue.
3. **End:** post a session log comment on each issue worked on:
   ```
   ### Session log — YYYY-MM-DD
   **Done:** …
   **Discoveries:** …
   **Decisions:** … (issue body updated: yes/no)
   **Next:** …
   **Links:** commits / PR / build report
   ```
   Tick completed checklist items in the issue body.

## Public repo hygiene

- Keep commercial plans, product naming and licensing discussion out of the repo, issues and commits. Those stay in the private spec doc (link in `CLAUDE.local.md`).
- Third-party CAM/post sources are design references only: reimplement, never copy code.
- Sanitize sample G-code before committing it: strip local file paths and usernames from header comments.

## Known pitfalls (details live in the feature issues)

- build123d `offset_3d` / Shell **segfaults** on the pointed-nose loft. Build cavities as an explicit inset loft. Run risky OCCT ops in a subprocess.
- After every boolean, assert `is_valid` and the expected solid/shell count. A sealed print section is 1 solid with 2 shells.
- 2.5D roundover fails on a single periodic closed spline. Build the outline as dorsal and ventral splines joined at nose and tail vertices, and clamp the radius with `max_fillet()`.
- Lofts: every section needs the same edge count and start point. Close the tips with a `Vertex`, not near-zero sections.
- OrcaSlicer: never mix the Snapmaker Orca fork's profiles with upstream's binary, or vice versa. The U1 stock process enables the prime tower (pass `--enable-prime-tower=0`). Feed STL, not 3MF. Boolean overrides use `--key=0`.
- Carbide Motion rejects G80–G83 (expand drilling into pecks) and ignores G54–59 (never emit G10/G92). `M6 Tn` triggers its tool-change prompt and the BitSetter. Post in mm. Snap arc endpoints to the output precision (avoids GRBL error 33).
