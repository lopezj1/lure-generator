# Deploying luregen on the home server

Development stays native (`uv run lure serve` + `npm run dev`). The server runs **one
container** built from `docker/Dockerfile`, managed by `docker/compose.yml` (#54, #69).

The image holds everything:

- the API (`lure serve`) and the built SPA;
- Python 3.13 with OCP/build123d;
- OrcaSlicer 2.5.1, extracted from its checksum-pinned AppImage;
- xvfb and tini.

The slicer runs **natively inside the container**. The image is Ubuntu 24.04, so the AppImage's
glibc 2.38 requirement is met with no Docker socket and no docker-in-docker.
`LUREGEN_SLICER_RUNTIME=host` is set, and `orca-slicer` is on `PATH`.

**Size:** about 2.65 GB on disk (about 1.0 GB gzipped). Adding the Snapmaker fallback brings it to
about 3.0 GB. Most of that is the pinned Python stack (1.24 GB venv: OCP 380 MB, scipy,
opencv, sympy, sklearn and about 170 MB of bytecode, kept so job subprocesses start fast as a
non-root user), OrcaSlicer (620 MB) and its GTK/WebKit libraries (about 500 MB).

**Build time:** a cold build takes about 8 minutes (measured 7.6 min, including the downloads). A
rebuild after a code-only change takes about 1 minute.

## Layout

| In the container | From the host (compose) | What |
|---|---|---|
| `/data/spec` | `../spec` | configs the UI edits (git-tracked; commit on the host) |
| `/data/builds` | `../builds` | build outputs |
| `/data/uploads` | `../uploads` | user uploads |
| `/data/web/dist` | (image) | built SPA, served at `/` |

The container runs as UID:GID 1000:1000 (user `luregen`), so files it writes on the volumes
belong to the host's first user. The container's clock is UTC, so build folder stamps are in UTC.
Don't mount a volume over the whole of `/data`: the built SPA
lives at `/data/web/dist` in the image.

## First run

```sh
git clone https://github.com/lopezj1/lure-generator.git && cd lure-generator
# Create the volume folders as your user, or Docker creates them as root.
mkdir -p builds uploads
GIT_SHA=$(git rev-parse --short HEAD) docker compose -f docker/compose.yml up -d --build
docker compose -f docker/compose.yml ps        # STATUS shows (healthy) after ~20 s
curl http://localhost:8000/api/health
```

Open `http://<server-lan-ip>:8000/`. If port 8000 is taken on the host, set `LUREGEN_PORT`
(e.g. `LUREGEN_PORT=8010 docker compose …`); the container still listens on 8000 inside.

The host user should be UID 1000. Otherwise, change `user:` in `docker/compose.yml` to
`"$(id -u):$(id -g)"`. Orca then has no home directory to write its config to, so also add
`environment: [HOME=/tmp]`.

Build arguments (set them as environment variables for `compose`, or pass `--build-arg` to
`docker build`):

| Arg | Default | Meaning |
|---|---|---|
| `GIT_SHA` | empty → `nogit` | commit shown by `/api/health` and stamped into build folders (`LUREGEN_GIT_SHA`) |
| `GIT_DIRTY` | `0` | `1` adds `-dirty` to the stamps (`LUREGEN_GIT_DIRTY`) |
| `WITH_SNAPMAKER` | `0` | `1` also extracts Snapmaker Orca 2.4.1 (`snapmaker-orca` on PATH), about +250 MB |

## LAN only

The app has **no authentication** (epic decision 4). Publish port 8000 on the LAN only. Never
port-forward it or expose it through a reverse proxy to the internet. To bind to a single
interface, change the port line to `"<lan-ip>:8000:8000"`.

## Update

```sh
git pull --ff-only
GIT_SHA=$(git rev-parse --short HEAD) docker compose -f docker/compose.yml up -d --build
docker image prune -f        # drop the previous image's dangling layers
```

`spec/` is the git checkout itself, so `git pull` also updates the configs. Commit any edits
made in the UI before pulling. The UI shows a badge when `spec/` has uncommitted changes.

## Backup

All state lives in the three host folders; the container itself is disposable.

- `spec/` is in git, so push your commits.
- `builds/` and `uploads/` are gitignored. Back them up with any file backup, e.g.
  ```sh
  tar czf luregen-data-$(date +%F).tar.gz builds uploads
  ```
  It's safe to do this while the container runs, though a job that is writing at that moment
  can be caught half-written.

Restore by extracting the archive into the checkout and running `docker compose up -d`.

## Commands inside the container

Every UI action is a CLI command, and they all work in the container (working directory `/data`):

```sh
docker compose -f docker/compose.yml exec luregen lure build spec/designs/glide/variants/striper_175_petg.toml
docker compose -f docker/compose.yml exec luregen lure fab builds/glide/striper_175_petg/latest --format print
```

## Tests in the image

The `test` target is the runtime image plus the dev dependency group, `tests/` and `spec/` in
`/app`. It runs the integration tests: a real OrcaSlicer slice of a 20 mm cube, plus the
profile sync from GitHub, which needs network.

```sh
docker build -f docker/Dockerfile --target test -t luregen:test .
docker run --rm luregen:test                 # pytest -m integration -q
```

`.github/workflows/image.yml` does the same: it runs on manual dispatch and on `v*` tags, and
also smoke-tests `/api/health`, the SPA and a `lure build` in the runtime image.
