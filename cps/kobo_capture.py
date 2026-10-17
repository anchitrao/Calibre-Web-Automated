# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Record raw Kobo device traffic for later analysis.

Large parts of the Kobo protocol (analytics events, UserStorage, annotation
payloads on newer firmware) are undocumented, and several of them are answered
by stubs that discard the body. Capturing the exchanges verbatim lets those
payloads be studied, and replayed into features, without guessing at schemas.

Captures go to their own SQLite file rather than app.db so that schema changes
here never interact with CWA's migrations, and the file can be deleted at any
time. This module only depends on the standard library and Flask so it can be
tested without importing the full application package.
"""

import logging
import os
import re
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/config/kobo_capture.db"
# Library sync pages can run to megabytes of entitlement JSON; the head of the
# body is enough to learn the shape, while device-originated bodies are kept whole.
MAX_REQUEST_BODY = 1024 * 1024
MAX_RESPONSE_BODY = 256 * 1024

# The device reaches CWA through two prefixes: the configured api_endpoint
# (/kobo/<auth token>/...) and reading_services_host, which the firmware reduces
# to scheme+host, so those calls land at the site root under /api/.
_KOBO_PREFIX = re.compile(r"^/kobo/[^/]+")
_ROOT_PREFIXES = ("/api/v3/", "/api/UserStorage/", "/api/internal/")
# Book files and covers are binary and already reproducible from the library;
# device database uploads are stored structurally by kobo_reading_data.
_SKIP = re.compile(r"^/kobo/[^/]+/(download/|v1/cwa/device-db$|.*\.jpg$)")

_HEADER_ALLOW = {"user-agent", "content-type", "content-encoding", "accept-encoding"}
# x-kobo-userkey identifies the Kobo account; everything else under x-kobo- is
# protocol state (sync tokens, API versions) that is needed to read the captures.
_HEADER_DENY = {"x-kobo-userkey", "authorization", "cookie", "set-cookie"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS exchange (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    query TEXT,
    status INTEGER,
    request_headers TEXT,
    request_body BLOB,
    request_size INTEGER,
    response_headers TEXT,
    response_body BLOB,
    response_size INTEGER
);
CREATE INDEX IF NOT EXISTS exchange_path_ts ON exchange (path, ts);
"""


def capture_enabled(environ=None):
    value = (environ if environ is not None else os.environ).get("CWA_KOBO_CAPTURE", "")
    return value.strip().lower() in ("1", "true", "yes", "on")


def is_capture_path(path):
    if _SKIP.match(path):
        return False
    return bool(_KOBO_PREFIX.match(path)) or path.startswith(_ROOT_PREFIXES)


def redact_path(path):
    return _KOBO_PREFIX.sub("/kobo/<token>", path, count=1)


def select_headers(headers):
    kept = {}
    for name, value in headers.items():
        lower = name.lower()
        if lower in _HEADER_DENY:
            continue
        if lower in _HEADER_ALLOW or lower.startswith("x-kobo-"):
            kept[name] = value
    return kept


def _headers_text(headers):
    return "\n".join(f"{name}: {value}" for name, value in sorted(headers.items()))


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=2)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def record_exchange(db_path, *, method, path, query, status,
                    request_headers, request_body, response_headers, response_body):
    """Store one exchange. Bodies are bytes (or None) and are truncated, not rejected."""
    request_body = request_body or b""
    response_body = response_body or b""
    conn = _connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO exchange (ts, method, path, query, status, request_headers, request_body,"
                " request_size, response_headers, response_body, response_size)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    method,
                    redact_path(path),
                    query or None,
                    status,
                    _headers_text(select_headers(request_headers)),
                    request_body[:MAX_REQUEST_BODY],
                    len(request_body),
                    _headers_text(select_headers(response_headers)),
                    response_body[:MAX_RESPONSE_BODY],
                    len(response_body),
                ),
            )
    finally:
        conn.close()


def make_after_request_hook(db_path=None):
    """Return a Flask after_request handler that records Kobo exchanges.

    The handler never raises and always returns the response unchanged: a
    capture failure must not turn into a sync failure on the device, which
    aborts the whole sync batch on any web request error.
    """
    db_path = db_path or os.environ.get("CWA_KOBO_CAPTURE_DB", DEFAULT_DB_PATH)

    def record(response):
        from flask import request

        try:
            if not is_capture_path(request.path):
                return response
            # Streamed and passthrough responses (file sends) cannot be read
            # without consuming them before they reach the device.
            if response.is_streamed or response.direct_passthrough:
                response_body = None
            else:
                response_body = response.get_data()
            record_exchange(
                db_path,
                method=request.method,
                path=request.path,
                query=request.query_string.decode("latin-1"),
                status=response.status_code,
                request_headers=request.headers,
                request_body=request.get_data(cache=True),
                response_headers=response.headers,
                response_body=response_body,
            )
        except Exception as e:
            log.warning("Kobo capture failed for %s: %s", request.path, e)
        return response

    return record
