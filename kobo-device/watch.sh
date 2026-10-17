#!/bin/sh
# Runs upload.sh after every Kobo sync. Nickel exposes no sync hook, but it
# rewrites LastSync= in its config when a sync completes, so a cheap poll of
# that one line is enough. A failed sync away from home normally leaves
# LastSync alone, and upload.sh treats an unreachable server as a skip anyway.
#
# Installed to the root filesystem (via KoboRoot.tgz) rather than the USB
# partition because that partition is unmounted while the Kobo is connected to
# a computer, which would kill a script being read from it.

# The CWA_* overrides exist so the script can be exercised off-device.
CONF=${CWA_CONF:-"/mnt/onboard/.kobo/Kobo/Kobo eReader.conf"}
UPLOAD=${CWA_UPLOAD:-/mnt/onboard/.adds/cwa/upload.sh}
LOCK=${CWA_LOCK:-/tmp/cwa-watch.lock}
POLL_SECONDS=${CWA_POLL_SECONDS:-60}
# Give Nickel time to finish writing KoboReader.sqlite after the sync; a copy
# taken mid-write fails the server's integrity check and waits for the next sync.
SETTLE_SECONDS=${CWA_SETTLE_SECONDS:-30}

# Creating this file on the USB partition turns the watcher off without having
# to remove anything from the root filesystem.
DISABLED=${CWA_DISABLED:-/mnt/onboard/.adds/cwa/disabled}

mkdir "$LOCK" 2>/dev/null || exit 0
echo $$ > "$LOCK/pid"

last=""
while :; do
    sleep "$POLL_SECONDS"
    if [ -f "$DISABLED" ]; then
        rm -rf "$LOCK"
        exit 0
    fi
    [ -f "$CONF" ] || continue
    current=$(sed -n 's/^LastSync=//p' "$CONF" | head -n 1)
    [ -n "$current" ] || continue
    if [ -z "$last" ]; then
        last=$current
        continue
    fi
    if [ "$current" != "$last" ]; then
        last=$current
        sleep "$SETTLE_SECONDS"
        [ -f "$UPLOAD" ] && /bin/sh "$UPLOAD"
    fi
done
