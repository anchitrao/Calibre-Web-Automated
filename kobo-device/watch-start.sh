#!/bin/sh
# Started by the udev rule early at boot. udev waits for (and may kill) the
# programs it runs, so the watcher is detached into its own session and this
# script returns immediately.

if command -v setsid >/dev/null 2>&1; then
    setsid /bin/sh /usr/local/cwa/watch.sh </dev/null >/dev/null 2>&1 &
else
    nohup /bin/sh /usr/local/cwa/watch.sh </dev/null >/dev/null 2>&1 &
fi
exit 0
