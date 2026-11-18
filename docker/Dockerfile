# syntax=docker/dockerfile:1
# luregen deploy image (#69): API + built SPA + Python/OCP + extracted OrcaSlicer, one container.
# Build from the repo root (see docs/deploy.md):
#   docker build -f docker/Dockerfile --build-arg GIT_SHA=$(git rev-parse --short HEAD) -t luregen .
#   docker build -f docker/Dockerfile --target test -t luregen:test .   # + dev deps and tests/
# Stages: web (node) → python (uv venv) → orca (AppImage extract) → base → test | runtime.

# --- 1. SPA ---------------------------------------------------------------------------------
FROM node:24-slim AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# --- 2. Python venv (uv-managed CPython in /opt/python, venv in /opt/venv) -------------------
FROM ghcr.io/astral-sh/uv:0.10.4 AS uv

FROM ubuntu:24.04 AS python
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock .python-version README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --group web --no-install-project
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --group web --no-editable

# --- 3. OrcaSlicer (pinned AppImages from spec/printers/snapmaker_u1/machine.toml) ----------
FROM ubuntu:24.04 AS orca
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      curl ca-certificates squashfs-tools \
    && rm -rf /var/lib/apt/lists/*
COPY docker/fetch-appimage.sh /usr/local/bin/fetch-appimage
ARG ORCA_URL=https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v2.5.1/OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.5.1.AppImage
ARG ORCA_SHA256=073b8105c928356141db0ddbce88b81d52c7a9e2442f9baae0a9089c89ff04eb
RUN fetch-appimage "$ORCA_URL" "$ORCA_SHA256" /opt/orca
# Optional fallback slicer (Snapmaker's fork), ~250 MB extracted: --build-arg WITH_SNAPMAKER=1.
ARG WITH_SNAPMAKER=0
ARG SNAPMAKER_URL=https://github.com/Snapmaker/OrcaSlicer/releases/download/v2.4.1/Snapmaker_Orca_Linux_AppImage_Ubuntu2404_V2.4.1.AppImage
ARG SNAPMAKER_SHA256=2b5117e22d04a3a27bcdfc5a873c55d28ff3b7ce9721946de1f134fa97d2cc06
RUN if [ "$WITH_SNAPMAKER" = 1 ]; then \
      fetch-appimage "$SNAPMAKER_URL" "$SNAPMAKER_SHA256" /opt/snapmaker-orca; \
    else mkdir -p /opt/snapmaker-orca; fi

# --- 4. Runtime base ------------------------------------------------------------------------
FROM ubuntu:24.04 AS base
# OrcaSlicer runtime libs (as tools/orca-runtime/Dockerfile), libgl1/libx11-6 for the OCP
# wheel, xvfb + xauth for `xvfb-run`, tini as PID 1 (xvfb-run hangs as PID 1).
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libgtk-3-0t64 libwebkit2gtk-4.1-0 libsecret-1-0 libfontconfig1 libxkbcommon0 \
      libopengl0 libegl1 libgl1 libglu1-mesa libgl1-mesa-dri libglx-mesa0 libwayland-egl1 \
      libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 libx11-6 \
      xvfb xauth tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# ubuntu:24.04 ships user `ubuntu` with UID/GID 1000; rename it so files on the volumes
# belong to the usual first user on the host.
RUN usermod -l luregen -d /home/luregen -m ubuntu && groupmod -n luregen ubuntu
COPY --from=python /opt/python /opt/python
COPY --from=python /opt/venv /opt/venv
COPY --from=orca /opt/orca /opt/orca
COPY --from=orca /opt/snapmaker-orca /opt/snapmaker-orca
# luregen.fab.printer.orca finds the slicer on PATH; AppRun resolves its own symlink.
RUN ln -s /opt/orca/AppRun /usr/local/bin/orca-slicer \
    && if [ -x /opt/snapmaker-orca/AppRun ]; then \
         ln -s /opt/snapmaker-orca/AppRun /usr/local/bin/snapmaker-orca; fi
# `lure serve --root /data` serves <root>/web/dist; spec/, builds/, uploads/ are volumes.
COPY --from=web --chown=1000:1000 /src/web/dist /data/web/dist
RUN mkdir -p /data/spec /data/builds /data/uploads && chown 1000:1000 /data /data/*
ENV PATH=/opt/venv/bin:$PATH \
    HOME=/home/luregen \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_PREFERENCE=only-managed \
    LUREGEN_SLICER_RUNTIME=host
# No .git in the image: build stamps and /api/health take the commit from here.
ARG GIT_SHA=
ARG GIT_DIRTY=0
ENV LUREGEN_GIT_SHA=$GIT_SHA \
    LUREGEN_GIT_DIRTY=$GIT_DIRTY
ENTRYPOINT ["/usr/bin/tini", "--"]

# --- 5a. Test target: dev deps + tests/ + spec/ in /app (pytest -m integration) -------------
FROM base AS test
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version README.md ./
COPY src/ src/
COPY tests/ tests/
COPY spec/ spec/
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    uv sync --frozen --group web --group dev --no-editable \
    && chown -R 1000:1000 /app
USER 1000:1000
CMD ["pytest", "-m", "integration", "-q"]

# --- 5b. Runtime (default target) -----------------------------------------------------------
FROM base AS runtime
USER 1000:1000
WORKDIR /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"]
CMD ["lure", "serve", "--host", "0.0.0.0", "--port", "8000", "--root", "/data"]
