#!/bin/sh
# Records what this Kobo's firmware provides (shell tools, HTTP clients) to
# probe.txt on the USB partition, to decide which code paths upload.sh can use.

OUT=/mnt/onboard/.adds/cwa/probe.txt

{
    date
    uname -a
    grep -m 3 -i -E 'model|hardware|processor' /proc/cpuinfo
    echo "--- busybox"
    busybox 2>&1 | head -n 1
    busybox --list 2>/dev/null | tr '\n' ' '
    echo
    echo "--- commands"
    for c in curl wget nc gzip sqlite3 openssl ip route; do
        printf '%s: %s\n' "$c" "$(command -v "$c" || echo missing)"
    done
    echo "--- wget options"
    wget --help 2>&1 | grep -E -- '--post|--header'
    echo "--- free space /tmp"
    df -k /tmp
    echo "--- reading-data watcher"
    ls -l /usr/local/cwa /etc/udev/rules.d/99-cwa-reading-data.rules 2>&1
    echo "lock: $(cat /tmp/cwa-watch.lock/pid 2>/dev/null || echo none)"
    ps 2>/dev/null | grep '[w]atch.sh' || echo "watch.sh not running"
    ls /dev/loop* 2>&1 | tr '\n' ' '
    echo
    tail -n 5 /mnt/onboard/.adds/cwa/watch.log 2>/dev/null
} > "$OUT" 2>&1

echo "Wrote $OUT"
