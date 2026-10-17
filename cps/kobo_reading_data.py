# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Keep the reading data a Kobo produces: annotations, progress, ratings, words.

Two sources feed the same tables:

* Sync traffic. Annotation PATCHes, reading-state PUTs and recommendation
  feedback are read as they pass through, without changing how CWA answers
  them. CWA's reading-services stub otherwise discards annotations entirely.
* Device database uploads. Some data never leaves the device over sync on
  4.x firmware (My Words, the Event counters behind Reading Life, highlights in
  books copied over USB), so an on-device script posts a gzipped SQLite file.

Both sources key annotations by the device's bookmark UUID, which is the same
value in the sync payload ("id") and in KoboReader.sqlite ("BookmarkID"), so a
highlight seen both ways is stored once.

Like kobo_capture, this module avoids importing the application package so it
can be tested in isolation; the caller injects how to resolve the current user.
"""

import gzip
import json
import logging
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/config/kobo_reading.db"
# A gap this long between two reading-state updates for the same book is taken
# to separate two sittings rather than one interrupted one.
SESSION_GAP_MINUTES = 30
MAX_UPLOAD_BYTES = 64 * 1024 * 1024

_ANNOTATIONS_PATH = re.compile(r"^/api/v3/content/(?P<book>[^/]+)/annotations$")
_STATE_PATH = re.compile(r"^/kobo/[^/]+/v1/library/(?P<book>[^/]+)/state$")
_FEEDBACK_PATH = re.compile(r"^/kobo/[^/]+/v1/user/recommendations/feedback$")
_RATING_FEEDBACK = re.compile(r"^Rate(?P<stars>[1-5])Star$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS annotation (
    id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    book_uuid TEXT,
    type TEXT,
    highlighted_text TEXT,
    note_text TEXT,
    highlight_color TEXT,
    chapter_title TEXT,
    chapter_filename TEXT,
    chapter_progress REAL,
    start_path TEXT,
    start_char INTEGER,
    end_path TEXT,
    end_char INTEGER,
    client_modified TEXT,
    deleted INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, id)
);
CREATE TABLE IF NOT EXISTS reading_state (
    user_id INTEGER NOT NULL,
    book_uuid TEXT NOT NULL,
    client_modified TEXT NOT NULL,
    progress_percent REAL,
    chapter_progress_percent REAL,
    location_source TEXT,
    location_type TEXT,
    location_value TEXT,
    spent_minutes INTEGER,
    remaining_minutes INTEGER,
    status TEXT,
    PRIMARY KEY (user_id, book_uuid, client_modified)
);
CREATE TABLE IF NOT EXISTS feedback (
    user_id INTEGER NOT NULL,
    book_uuid TEXT NOT NULL,
    feedback_type TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (user_id, book_uuid, feedback_type)
);
CREATE TABLE IF NOT EXISTS word (
    user_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    volume_id TEXT,
    dict_suffix TEXT,
    date_created TEXT,
    received_at TEXT NOT NULL,
    PRIMARY KEY (user_id, text)
);
CREATE TABLE IF NOT EXISTS event_counter (
    user_id INTEGER NOT NULL,
    content_id TEXT NOT NULL,
    event_type INTEGER NOT NULL,
    first_occurrence TEXT,
    last_occurrence TEXT,
    event_count INTEGER,
    extra_data BLOB,
    received_at TEXT NOT NULL,
    PRIMARY KEY (user_id, content_id, event_type)
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def normalize_timestamp(value):
    """Return value as UTC ISO-8601 with microseconds, so timestamps sort as text.

    Sync payloads use second precision with a Z suffix ("...:11Z") while the
    device database uses naive milliseconds ("...:11.806"); compared raw, '.'
    sorts before 'Z' and the later edit would lose. Naive values are UTC.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def reading_data_enabled(environ=None):
    value = (environ if environ is not None else os.environ).get("CWA_KOBO_READING_DATA", "")
    return value.strip().lower() in ("1", "true", "yes", "on")


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    # Rows written before timestamps were normalized must be brought into the
    # same form, or a raw "...Z" value would compare as newer than any update.
    conn.create_function("cwa_normalize_timestamp", 1, normalize_timestamp, deterministic=True)
    with conn:
        conn.execute(
            "UPDATE annotation SET client_modified = cwa_normalize_timestamp(client_modified)"
            " WHERE client_modified IS NOT NULL AND client_modified NOT LIKE '%+00:00'"
        )
    return conn


def upsert_annotation(conn, user_id, row, source):
    """Insert or update one annotation, keeping whichever copy was edited last.

    On a tie the sync copy wins, since it carries the chapter title and file that
    the device database lacks; coalesce() keeps those fields either way.
    """
    conn.execute(
        """
        INSERT INTO annotation (id, user_id, book_uuid, type, highlighted_text, note_text,
            highlight_color, chapter_title, chapter_filename, chapter_progress, start_path,
            start_char, end_path, end_char, client_modified, deleted, source, updated_at)
        VALUES (:id, :user_id, :book_uuid, :type, :highlighted_text, :note_text,
            :highlight_color, :chapter_title, :chapter_filename, :chapter_progress, :start_path,
            :start_char, :end_path, :end_char, :client_modified, 0, :source, :updated_at)
        ON CONFLICT (user_id, id) DO UPDATE SET
            book_uuid = coalesce(excluded.book_uuid, annotation.book_uuid),
            type = excluded.type,
            highlighted_text = excluded.highlighted_text,
            note_text = excluded.note_text,
            highlight_color = coalesce(excluded.highlight_color, annotation.highlight_color),
            chapter_title = coalesce(excluded.chapter_title, annotation.chapter_title),
            chapter_filename = coalesce(excluded.chapter_filename, annotation.chapter_filename),
            chapter_progress = coalesce(excluded.chapter_progress, annotation.chapter_progress),
            start_path = coalesce(excluded.start_path, annotation.start_path),
            start_char = coalesce(excluded.start_char, annotation.start_char),
            end_path = coalesce(excluded.end_path, annotation.end_path),
            end_char = coalesce(excluded.end_char, annotation.end_char),
            client_modified = excluded.client_modified,
            deleted = 0,
            source = excluded.source,
            updated_at = excluded.updated_at
        WHERE annotation.client_modified IS NULL
           OR excluded.client_modified > annotation.client_modified
           OR (excluded.client_modified = annotation.client_modified AND excluded.source = 'sync')
        """,
        {**row, "client_modified": normalize_timestamp(row["client_modified"]),
         "user_id": user_id, "source": source, "updated_at": _now()},
    )


def ingest_annotations_patch(conn, user_id, book_uuid, payload):
    for item in payload.get("updatedAnnotations") or []:
        if not item.get("id"):
            continue
        span = (item.get("location") or {}).get("span") or {}
        upsert_annotation(conn, user_id, {
            "id": item["id"],
            "book_uuid": book_uuid,
            "type": item.get("type"),
            "highlighted_text": item.get("highlightedText"),
            "note_text": item.get("noteText"),
            "highlight_color": item.get("highlightColor"),
            "chapter_title": span.get("chapterTitle"),
            "chapter_filename": span.get("chapterFilename"),
            "chapter_progress": span.get("chapterProgress"),
            "start_path": span.get("startPath"),
            "start_char": span.get("startChar"),
            "end_path": span.get("endPath"),
            "end_char": span.get("endChar"),
            "client_modified": item.get("clientLastModifiedUtc"),
        }, source="sync")
    for annotation_id in payload.get("deletedAnnotationIds") or []:
        conn.execute(
            "UPDATE annotation SET deleted = 1, updated_at = ? WHERE user_id = ? AND id = ?",
            (_now(), user_id, annotation_id),
        )


def ingest_reading_state(conn, user_id, payload):
    for state in payload.get("ReadingStates") or []:
        book_uuid = state.get("EntitlementId")
        modified = state.get("LastModified")
        if not book_uuid or not modified:
            continue
        bookmark = state.get("CurrentBookmark") or {}
        location = bookmark.get("Location") or {}
        stats = state.get("Statistics") or {}
        status = (state.get("StatusInfo") or {}).get("Status")
        conn.execute(
            """
            INSERT OR IGNORE INTO reading_state (user_id, book_uuid, client_modified,
                progress_percent, chapter_progress_percent, location_source, location_type,
                location_value, spent_minutes, remaining_minutes, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, book_uuid, modified,
                bookmark.get("ProgressPercent"), bookmark.get("ContentSourceProgressPercent"),
                location.get("Source"), location.get("Type"), location.get("Value"),
                stats.get("SpentReadingMinutes"), stats.get("RemainingTimeMinutes"), status,
            ),
        )


def ingest_feedback(conn, user_id, payload, received_at=None):
    for item in payload.get("FeedbackItems") or []:
        book_uuid, feedback_type = item.get("RevisionId"), item.get("FeedbackType")
        if not book_uuid or not feedback_type:
            continue
        if _RATING_FEEDBACK.match(feedback_type):
            # A new star rating replaces the old one rather than accumulating.
            conn.execute(
                "DELETE FROM feedback WHERE user_id = ? AND book_uuid = ? AND feedback_type LIKE 'Rate_Star'",
                (user_id, book_uuid),
            )
        conn.execute(
            "INSERT OR REPLACE INTO feedback (user_id, book_uuid, feedback_type, received_at) VALUES (?, ?, ?, ?)",
            (user_id, book_uuid, feedback_type, received_at or _now()),
        )


def ingest_exchange(conn, user_id, method, path, body, received_at=None):
    """Route one device request to the matching ingester. Returns True if it was used."""
    if not body:
        return False
    match = _ANNOTATIONS_PATH.match(path)
    if match and method == "PATCH":
        ingest_annotations_patch(conn, user_id, match.group("book"), json.loads(body))
        return True
    if _STATE_PATH.match(path) and method == "PUT":
        ingest_reading_state(conn, user_id, json.loads(body))
        return True
    if _FEEDBACK_PATH.match(path) and method == "POST":
        ingest_feedback(conn, user_id, json.loads(body), received_at)
        return True
    return False


def backfill_from_capture(capture_db_path, conn, user_id):
    """Replay exchanges already recorded by kobo_capture. Safe to run repeatedly."""
    if not os.path.exists(capture_db_path):
        return 0
    capture = sqlite3.connect(f"file:{capture_db_path}?mode=ro", uri=True)
    used = 0
    try:
        rows = capture.execute(
            "SELECT ts, method, path, request_body FROM exchange"
            " WHERE method IN ('PATCH', 'PUT', 'POST') ORDER BY id"
        ).fetchall()
    finally:
        capture.close()
    with conn:
        for ts, method, path, body in rows:
            path = path.replace("/kobo/<token>/", "/kobo/token/", 1)
            try:
                used += ingest_exchange(conn, user_id, method, path, body, received_at=ts)
            except (ValueError, TypeError) as e:
                log.warning("Skipping unreadable captured exchange %s %s: %s", method, path, e)
    return used


def ingest_device_database(conn, user_id, device_db_path):
    """Import My Words, Event counters and bookmarks from a copy of KoboReader.sqlite.

    The upload may be the full database or a subset built on the device, so
    every table is optional.
    """
    device = sqlite3.connect(f"file:{device_db_path}?mode=ro", uri=True)
    device.row_factory = sqlite3.Row
    counts = {"words": 0, "events": 0, "annotations": 0}
    received = _now()
    try:
        tables = {r[0] for r in device.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        with conn:
            if "WordList" in tables:
                for r in device.execute("SELECT Text, VolumeId, DictSuffix, DateCreated FROM WordList"):
                    conn.execute(
                        """
                        INSERT INTO word (user_id, text, volume_id, dict_suffix, date_created, received_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT (user_id, text) DO UPDATE SET
                            volume_id = excluded.volume_id, dict_suffix = excluded.dict_suffix,
                            date_created = excluded.date_created
                        """,
                        (user_id, r["Text"], r["VolumeId"], r["DictSuffix"],
                         r["DateCreated"], received),
                    )
                    counts["words"] += 1
            if "Event" in tables:
                # The device writes binary ExtraData with TEXT affinity, which the
                # sqlite3 module would otherwise try (and fail) to decode as UTF-8.
                for r in device.execute(
                    "SELECT EventType, FirstOccurrence, LastOccurrence, EventCount, ContentID,"
                    " CAST(ExtraData AS BLOB) AS ExtraData FROM Event"
                ):
                    conn.execute(
                        "INSERT OR REPLACE INTO event_counter (user_id, content_id, event_type, first_occurrence,"
                        " last_occurrence, event_count, extra_data, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (user_id, r["ContentID"] or "", r["EventType"], r["FirstOccurrence"],
                         r["LastOccurrence"], r["EventCount"], r["ExtraData"], received),
                    )
                    counts["events"] += 1
            if "Bookmark" in tables:
                for r in device.execute(
                    "SELECT BookmarkID, VolumeID, Text, Annotation, Type, ChapterProgress, StartContainerPath,"
                    " StartOffset, EndContainerPath, EndOffset, DateCreated, DateModified, Hidden FROM Bookmark"
                ):
                    if r["Hidden"] in (1, "true"):
                        continue
                    # Bookmarks from sync already carry richer location data, so the
                    # device copy only fills gaps (see the coalesce() in upsert_annotation).
                    upsert_annotation(conn, user_id, {
                        "id": r["BookmarkID"],
                        # Books delivered by sync use the Calibre UUID as VolumeID; books
                        # copied over USB use a file:// path, kept verbatim for grouping.
                        "book_uuid": r["VolumeID"],
                        "type": r["Type"],
                        "highlighted_text": r["Text"],
                        "note_text": r["Annotation"] or None,
                        "highlight_color": None,
                        "chapter_title": None,
                        "chapter_filename": None,
                        "chapter_progress": r["ChapterProgress"],
                        "start_path": r["StartContainerPath"],
                        "start_char": r["StartOffset"],
                        "end_path": r["EndContainerPath"],
                        "end_char": r["EndOffset"],
                        "client_modified": r["DateModified"] or r["DateCreated"],
                    }, source="device")
                    counts["annotations"] += 1
    finally:
        device.close()
    return counts


def ingest_device_upload(conn, user_id, body):
    """Accept a gzipped (or plain) SQLite file posted by the on-device script."""
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    if not body.startswith(b"SQLite format 3\x00"):
        raise ValueError("upload is not a SQLite database")
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
        tmp.write(body)
        tmp.flush()
        check = sqlite3.connect(f"file:{tmp.name}?mode=ro", uri=True)
        try:
            # The device copies its live database, so a write racing the copy can
            # leave a torn file; refuse it rather than import half a table.
            if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("uploaded database failed integrity check")
        finally:
            check.close()
        return ingest_device_database(conn, user_id, tmp.name)


def _parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def reading_sessions(conn, user_id, gap_minutes=SESSION_GAP_MINUTES):
    """Derive sittings from successive reading-state updates.

    The device reports cumulative SpentReadingMinutes, so the minutes of a
    sitting are the growth across a run of updates no more than gap_minutes
    apart. Runs whose total does not grow (re-syncs, opening a book without
    reading) are dropped.
    """
    rows = conn.execute(
        "SELECT book_uuid, client_modified, spent_minutes, progress_percent FROM reading_state"
        " WHERE user_id = ? AND spent_minutes IS NOT NULL ORDER BY book_uuid, client_modified",
        (user_id,),
    ).fetchall()
    sessions = []
    previous = None
    current = None
    for row in rows:
        ts = _parse_ts(row["client_modified"])
        same_run = (
            previous is not None
            and previous["book_uuid"] == row["book_uuid"]
            and (ts - previous["ts"]).total_seconds() <= gap_minutes * 60
        )
        if not same_run:
            if current and current["minutes"] > 0:
                sessions.append(current)
            baseline = previous["spent"] if previous is not None and previous["book_uuid"] == row["book_uuid"] else None
            current = {
                "book_uuid": row["book_uuid"],
                "start": ts,
                "end": ts,
                "minutes": max(0, row["spent_minutes"] - baseline) if baseline is not None else 0,
                "progress_start": row["progress_percent"],
                "progress_end": row["progress_percent"],
            }
        else:
            current["end"] = ts
            current["minutes"] += max(0, row["spent_minutes"] - previous["spent"])
            current["progress_end"] = row["progress_percent"]
        previous = {"book_uuid": row["book_uuid"], "ts": ts, "spent": row["spent_minutes"]}
    if current and current["minutes"] > 0:
        sessions.append(current)
    sessions.sort(key=lambda s: s["end"])
    return sessions


def make_ingest_hook(resolve_user_id, db_path=None):
    """Return a Flask after_request handler that stores reading data from sync traffic.

    resolve_user_id() returns the CWA user the request belongs to, or None. As
    with kobo_capture, failures are logged and the response is always returned.
    """
    db_path = db_path or os.environ.get("CWA_KOBO_READING_DB", DEFAULT_DB_PATH)

    def ingest(response):
        from flask import request

        try:
            if request.method not in ("PATCH", "PUT", "POST") or response.status_code >= 500:
                return response
            path = request.path
            if not (_ANNOTATIONS_PATH.match(path) or _STATE_PATH.match(path) or _FEEDBACK_PATH.match(path)):
                return response
            user_id = resolve_user_id()
            if user_id is None:
                return response
            conn = connect(db_path)
            try:
                with conn:
                    ingest_exchange(conn, user_id, request.method, path, request.get_data(cache=True))
            finally:
                conn.close()
        except Exception as e:
            log.warning("Kobo reading data ingest failed for %s: %s", request.path, e)
        return response

    return ingest
