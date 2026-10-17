#!/bin/sh
# Builds a KoboRoot.tgz containing the official NickelMenu release plus the
# reading-data watcher. Nickel applies only one KoboRoot.tgz per boot, so both
# have to be in the same archive.
#
#   sh build-koboroot.sh path/to/NickelMenu/KoboRoot.tgz out/KoboRoot.tgz
#   sh build-koboroot.sh none out/KoboRoot.tgz   # NickelMenu already installed
#
# Nickel extracts the archive over /, so it must hold file entries only, like
# the NickelMenu release does: a directory entry would reset that directory's
# mode on the device (mktemp's 0700 on "./" would lock everyone else out of /).
# Entries are owned by root and written without macOS metadata, since bsdtar
# would otherwise add ._* AppleDouble files to the Kobo's root filesystem.

set -eu

nickelmenu=$1
out=$2
here=$(cd "$(dirname "$0")" && pwd)
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT

if [ "$nickelmenu" != none ]; then
    tar -xzf "$nickelmenu" -C "$stage"
fi
install -d "$stage/etc/udev/rules.d" "$stage/usr/local/cwa"
install -m 0644 "$here/99-cwa-reading-data.rules" "$stage/etc/udev/rules.d/"
install -m 0755 "$here/watch.sh" "$here/watch-start.sh" "$stage/usr/local/cwa/"

files=$(cd "$stage" && find . -type f | sort)
# shellcheck disable=SC2086
COPYFILE_DISABLE=1 tar -czf "$out" -C "$stage" -n \
    --uid 0 --gid 0 --uname root --gname root --no-mac-metadata --no-xattrs \
    --format ustar $files
if tar -tzvf "$out" | grep -v '^-'; then
    echo "refusing: archive contains non-file entries" >&2
    rm -f "$out"
    exit 1
fi
tar -tzvf "$out"
