# Runtime for the OrcaSlicer AppImages (built on Ubuntu 24.04: glibc 2.38, GTK 3, WebKitGTK 4.1).
# Used by luregen.fab.printer.orca when the host is older (e.g. Debian 12). The extracted
# AppImage is bind-mounted from the cache, so this image holds only the shared libraries.
#   docker build -t luregen/orca-runtime:24.04 tools/orca-runtime
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libgtk-3-0t64 libwebkit2gtk-4.1-0 libsecret-1-0 libfontconfig1 libxkbcommon0 \
      libopengl0 libegl1 libgl1 libglu1-mesa libgl1-mesa-dri libglx-mesa0 libwayland-egl1 \
      libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 \
      xvfb xauth ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ENV HOME=/tmp
