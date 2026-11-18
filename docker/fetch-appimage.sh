#!/bin/sh
# Download an AppImage, check its sha256 and extract it to <dest> (used by docker/Dockerfile).
#   fetch-appimage.sh <url> <sha256> <dest>
# `--appimage-extract` runs the AppImage's own runtime; if that fails (e.g. it is blocked in
# the build sandbox), fall back to unsquashfs at the squashfs offset, which is the end of the
# type-2 runtime ELF: e_shoff + e_shentsize * e_shnum (read from the header, nothing executed).
set -eu
url=$1 sha=$2 dest=$3
work=$(mktemp -d)
cd "$work"
curl -fsSL --retry 3 -o app.AppImage "$url"
echo "$sha  app.AppImage" | sha256sum -c -
chmod +x app.AppImage
if ! ./app.AppImage --appimage-extract >/dev/null 2>&1; then
    rm -rf squashfs-root
    shoff=$(od -An -t u8 -j 40 -N 8 app.AppImage)
    shentsize=$(od -An -t u2 -j 58 -N 2 app.AppImage)
    shnum=$(od -An -t u2 -j 60 -N 2 app.AppImage)
    offset=$((shoff + shentsize * shnum))
    unsquashfs -q -o "$offset" -d squashfs-root app.AppImage
fi
test -x squashfs-root/AppRun
mkdir -p "$(dirname "$dest")"
mv squashfs-root "$dest"
# Extracted trees are 0700; the app runs as a non-root user.
chmod -R a+rX,go-w "$dest"
cd / && rm -rf "$work"
