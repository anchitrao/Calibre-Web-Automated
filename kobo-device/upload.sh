#!/bin/sh
# Sends KoboReader.sqlite to CWA's /v1/cwa/device-db so data the 4.x firmware
# never syncs (My Words, Reading Life counters, highlights in USB-loaded books)
# reaches the server. Run by watch.sh after each Kobo sync; the outcome goes to
# $STATUS for the "Reading data status" NickelMenu item.
#
# The CWA URL and auth token come from the api_endpoint the Kobo already syncs
# with, so nothing secret is stored here and a re-paired device keeps working.
#
# CWA is only reachable from the home network. Not reaching it is therefore an
# expected outcome, reported as "Skipped" with exit status 0; only a server that
# answers and refuses the upload counts as a failure.

set -u

# The CWA_* overrides exist so the script can be exercised off-device.
DIR=${CWA_DIR:-/mnt/onboard/.adds/cwa}
DB=${CWA_DB:-/mnt/onboard/.kobo/KoboReader.sqlite}
CONF=${CWA_CONF:-"/mnt/onboard/.kobo/Kobo/Kobo eReader.conf"}
TMP=${CWA_TMP:-/tmp/cwa-device-db.gz}
STATUS="$DIR/last_upload.txt"
CONNECT_TIMEOUT=5
# Long enough for the server to import a full database before answering.
RESPONSE_TIMEOUT=60

status() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" > "$STATUS"
}

cleanup() {
    rm -f "$TMP"
}
trap cleanup EXIT

endpoint=$(sed -n 's/^api_endpoint=//p' "$CONF" | head -n 1)
case "$endpoint" in
    http://*/kobo/*) ;;
    https://*/kobo/*)
        status "Failed: busybox clients cannot do HTTPS; api_endpoint must be http:// ($endpoint)"
        exit 1 ;;
    *)
        status "Skipped: this Kobo is not syncing with CWA (api_endpoint=$endpoint)"
        exit 0 ;;
esac
url="$endpoint/v1/cwa/device-db"
hostport=$(echo "$url" | sed -e 's#^http://##' -e 's#/.*##')
host=${hostport%%:*}
port=80
case "$hostport" in *:*) port=${hostport##*:} ;; esac

reachable() {
    if command -v curl >/dev/null 2>&1; then
        curl -s -o /dev/null --connect-timeout "$CONNECT_TIMEOUT" "http://$hostport/"
    else
        nc -z -w "$CONNECT_TIMEOUT" "$host" "$port" 2>/dev/null </dev/null
    fi
}

# Prints the HTTP status code of the response, or nothing if no response came back.
post_nc() {
    path="/${url#http://*/}"
    # HTTP/1.0 makes the server close the connection after responding, which is
    # what lets busybox nc exit once the status line has been read.
    {
        printf 'POST %s HTTP/1.0\r\nHost: %s\r\nContent-Type: application/gzip\r\nContent-Length: %s\r\n\r\n' \
            "$path" "$hostport" "$size"
        cat "$TMP"
    } | nc -w "$RESPONSE_TIMEOUT" "$host" "$port" 2>/dev/null | head -n 1 | awk '{print $2}'
}

post() {
    method=${CWA_POST_METHOD:-}
    if [ -z "$method" ]; then
        if command -v curl >/dev/null 2>&1; then method=curl; else method=nc; fi
    fi
    case "$method" in
        curl)
            curl -s -o /dev/null -w '%{http_code}' --connect-timeout "$CONNECT_TIMEOUT" \
                --max-time "$RESPONSE_TIMEOUT" -X POST -H 'Content-Type: application/gzip' \
                --data-binary "@$TMP" "$url" ;;
        *)
            post_nc ;;
    esac
}

if ! reachable; then
    status "Skipped: CWA at $hostport is not reachable from this network"
    exit 0
fi

gzip -c "$DB" > "$TMP" || { status "Failed: could not read $DB"; exit 1; }
size=$(wc -c < "$TMP" | tr -d ' ')

code=$(post)
case "$code" in
    200)
        status "Uploaded $size bytes" ;;
    ""|000)
        status "Skipped: lost the connection to $hostport during upload"
        exit 0 ;;
    *)
        status "Failed: CWA answered HTTP $code"
        exit 1 ;;
esac
