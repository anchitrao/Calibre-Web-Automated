# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Unit tests for storing Kobo reading data from sync traffic and device uploads."""

import gzip
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from flask import Flask, jsonify


def _load(name):
    module_path = Path(__file__).resolve().parents[2] / "cps" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reading = _load("kobo_reading_data")

BOOK = "24f343d2-4124-46d5-8ba7-1ba7a638ef2f"


def _annotation(annotation_id, modified, text="A passage.", note=None, kind="highlight"):
    item = {
        "id": annotation_id,
        "clientLastModifiedUtc": modified,
        "highlightedText": text,
        "type": kind,
        "location": {"span": {
            "chapterFilename": "OEBPS/c001.htm",
            "chapterProgress": 0.5,
            "chapterTitle": "Prologue",
            "startPath": "span#kobo\\.19\\.1",
            "startChar": 0,
            "endPath": "span#kobo\\.19\\.5",
            "endChar": 151,
        }},
    }
    if note is not None:
        item["noteText"] = note
    return item


def _state(modified, spent, progress=10):
    return {"ReadingStates": [{
        "EntitlementId": BOOK,
        "LastModified": modified,
        "CurrentBookmark": {
            "ProgressPercent": progress,
            "ContentSourceProgressPercent": 33,
            "Location": {"Source": "OEBPS/c001.htm", "Type": "KoboSpan", "Value": "kobo.14.4"},
        },
        "Statistics": {"SpentReadingMinutes": spent, "RemainingTimeMinutes": 200},
        "StatusInfo": {"Status": "Reading"},
    }]}


@pytest.fixture
def conn(tmp_path):
    c = reading.connect(str(tmp_path / "reading.db"))
    yield c
    c.close()


def _annotations(conn):
    return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM annotation")}


@pytest.mark.unit
class TestAnnotations:
    def test_patch_stores_text_note_and_location(self, conn):
        reading.ingest_annotations_patch(conn, 1, BOOK, {"updatedAnnotations": [
            _annotation("a1", "2026-10-17T05:42:47Z", note="Rest", kind="note"),
        ]})
        row = _annotations(conn)["a1"]
        assert row["book_uuid"] == BOOK
        assert row["note_text"] == "Rest"
        assert row["type"] == "note"
        assert row["chapter_title"] == "Prologue"
        assert row["start_path"] == "span#kobo\\.19\\.1"
        assert row["end_char"] == 151
        assert row["source"] == "sync"

    def test_newer_edit_wins_and_older_replay_is_ignored(self, conn):
        reading.ingest_annotations_patch(conn, 1, BOOK, {"updatedAnnotations": [
            _annotation("a1", "2026-10-17T05:00:00Z", note="first")]})
        reading.ingest_annotations_patch(conn, 1, BOOK, {"updatedAnnotations": [
            _annotation("a1", "2026-10-17T06:00:00Z", note="second")]})
        reading.ingest_annotations_patch(conn, 1, BOOK, {"updatedAnnotations": [
            _annotation("a1", "2026-10-17T05:30:00Z", note="stale")]})
        assert _annotations(conn)["a1"]["note_text"] == "second"

    def test_deleted_ids_are_soft_deleted(self, conn):
        reading.ingest_annotations_patch(conn, 1, BOOK, {"updatedAnnotations": [
            _annotation("a1", "2026-10-17T05:00:00Z")]})
        reading.ingest_annotations_patch(conn, 1, BOOK, {"deletedAnnotationIds": ["a1"]})
        assert _annotations(conn)["a1"]["deleted"] == 1

    def test_users_are_kept_apart(self, conn):
        payload = {"updatedAnnotations": [_annotation("a1", "2026-10-17T05:00:00Z")]}
        reading.ingest_annotations_patch(conn, 1, BOOK, payload)
        reading.ingest_annotations_patch(conn, 2, BOOK, payload)
        assert conn.execute("SELECT count(*) FROM annotation").fetchone()[0] == 2


@pytest.mark.unit
class TestReadingStateAndSessions:
    def test_duplicate_state_updates_are_stored_once(self, conn):
        reading.ingest_reading_state(conn, 1, _state("2026-10-17T05:35:02Z", 16))
        reading.ingest_reading_state(conn, 1, _state("2026-10-17T05:35:02Z", 16))
        row = conn.execute("SELECT * FROM reading_state").fetchone()
        assert conn.execute("SELECT count(*) FROM reading_state").fetchone()[0] == 1
        assert (row["spent_minutes"], row["location_value"], row["status"]) == (16, "kobo.14.4", "Reading")

    def test_sessions_split_on_gap_and_sum_minute_growth(self, conn):
        for ts, spent, progress in [
            ("2026-10-16T20:00:00Z", 10, 1),
            ("2026-10-16T20:20:00Z", 25, 3),
            ("2026-10-16T20:40:00Z", 40, 5),
            ("2026-10-17T07:00:00Z", 40, 5),
            ("2026-10-17T07:15:00Z", 52, 7),
        ]:
            reading.ingest_reading_state(conn, 1, _state(ts, spent, progress))
        sessions = reading.reading_sessions(conn, 1)
        assert [s["minutes"] for s in sessions] == [30, 12]
        assert (sessions[0]["progress_start"], sessions[0]["progress_end"]) == (1, 5)
        assert sessions[1]["start"].hour == 7

    def test_resync_without_reading_is_not_a_session(self, conn):
        reading.ingest_reading_state(conn, 1, _state("2026-10-16T20:00:00Z", 10))
        reading.ingest_reading_state(conn, 1, _state("2026-10-17T09:00:00Z", 10))
        assert reading.reading_sessions(conn, 1) == []


@pytest.mark.unit
class TestFeedback:
    def test_new_star_rating_replaces_old(self, conn):
        reading.ingest_feedback(conn, 1, {"FeedbackItems": [{"FeedbackType": "Rate4Star", "RevisionId": BOOK}]})
        reading.ingest_feedback(conn, 1, {"FeedbackItems": [{"FeedbackType": "Rate2Star", "RevisionId": BOOK}]})
        rows = conn.execute("SELECT feedback_type FROM feedback").fetchall()
        assert [r[0] for r in rows] == ["Rate2Star"]


def _device_db(path, *, hidden=False):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE WordList (Text TEXT PRIMARY KEY, VolumeId TEXT, DictSuffix TEXT, DateCreated TEXT);
        CREATE TABLE Event (EventType INTEGER, FirstOccurrence TEXT, LastOccurrence TEXT,
            EventCount INTEGER, ContentID TEXT, ExtraData BLOB, Checksum TEXT);
        CREATE TABLE Bookmark (BookmarkID TEXT, VolumeID TEXT, Text TEXT, Annotation TEXT, Type TEXT,
            ChapterProgress REAL, StartContainerPath TEXT, StartOffset INTEGER, EndContainerPath TEXT,
            EndOffset INTEGER, DateCreated TEXT, DateModified TEXT, Hidden TEXT);
    """)
    db.execute("INSERT INTO WordList VALUES ('programme', ?, '-en', '2026-10-17T05:35:19Z')", (BOOK,))
    # Stored the way the device does it: non-UTF-8 bytes in a TEXT value.
    db.execute("INSERT INTO Event VALUES (46, '2024-01-03', '2026-10-17', 12, ?, CAST(X'ff0102' AS TEXT), NULL)",
               (BOOK,))
    db.execute(
        "INSERT INTO Bookmark VALUES ('a1', ?, 'A passage.', '', 'highlight', 0.5, 'span#kobo\\.19\\.1', 0,"
        " 'span#kobo\\.19\\.5', 151, '2026-10-17T05:42:47.000', NULL, ?)",
        (BOOK, "true" if hidden else "false"),
    )
    db.execute(
        "INSERT INTO Bookmark VALUES ('usb1', 'file:///mnt/onboard/book.epub', 'From a USB book.', 'mine',"
        " 'note', 0.1, 'span#kobo\\.2\\.1', 0, 'span#kobo\\.2\\.3', 10, '2024-05-10T16:44:23.000', NULL, 'false')"
    )
    db.commit()
    db.close()


@pytest.mark.unit
class TestDeviceUpload:
    def test_gzipped_upload_imports_words_events_and_bookmarks(self, conn, tmp_path):
        _device_db(tmp_path / "KoboReader.sqlite")
        body = gzip.compress((tmp_path / "KoboReader.sqlite").read_bytes())
        counts = reading.ingest_device_upload(conn, 1, body)
        assert counts == {"words": 1, "events": 1, "annotations": 2}
        word = conn.execute("SELECT * FROM word").fetchone()
        assert (word["text"], word["volume_id"]) == ("programme", BOOK)
        event = conn.execute("SELECT * FROM event_counter").fetchone()
        assert (event["event_type"], event["event_count"], event["extra_data"]) == (46, 12, b"\xff\x01\x02")
        usb = _annotations(conn)["usb1"]
        assert (usb["book_uuid"], usb["note_text"], usb["source"]) == (
            "file:///mnt/onboard/book.epub", "mine", "device")

    def test_device_copy_keeps_richer_sync_location(self, conn, tmp_path):
        reading.ingest_annotations_patch(conn, 1, BOOK, {"updatedAnnotations": [
            _annotation("a1", "2026-10-17T05:42:47Z")]})
        _device_db(tmp_path / "KoboReader.sqlite")
        reading.ingest_device_upload(conn, 1, (tmp_path / "KoboReader.sqlite").read_bytes())
        row = _annotations(conn)["a1"]
        assert row["chapter_title"] == "Prologue"
        assert row["chapter_filename"] == "OEBPS/c001.htm"

    def test_sync_copy_wins_a_timestamp_tie(self, conn):
        reading.ingest_annotations_patch(conn, 1, BOOK, {"updatedAnnotations": [
            _annotation("a1", "2026-10-17T05:42:47Z", text="from sync")]})
        reading.upsert_annotation(conn, 1, {
            "id": "a1", "book_uuid": BOOK, "type": "highlight", "highlighted_text": "from device",
            "note_text": None, "highlight_color": None, "chapter_title": None, "chapter_filename": None,
            "chapter_progress": None, "start_path": None, "start_char": None, "end_path": None,
            "end_char": None, "client_modified": "2026-10-17T05:42:47.000",
        }, source="device")
        row = _annotations(conn)["a1"]
        assert (row["highlighted_text"], row["source"]) == ("from sync", "sync")

    def test_later_device_edit_within_the_same_second_wins(self, conn):
        reading.ingest_annotations_patch(conn, 1, BOOK, {"updatedAnnotations": [
            _annotation("a1", "2026-10-17T05:42:47Z", note="old")]})
        reading.upsert_annotation(conn, 1, {
            "id": "a1", "book_uuid": BOOK, "type": "note", "highlighted_text": "A passage.",
            "note_text": "edited", "highlight_color": None, "chapter_title": None, "chapter_filename": None,
            "chapter_progress": None, "start_path": None, "start_char": None, "end_path": None,
            "end_char": None, "client_modified": "2026-10-17T05:42:47.806",
        }, source="device")
        row = _annotations(conn)["a1"]
        assert row["note_text"] == "edited"
        assert row["chapter_title"] == "Prologue"

    def test_connect_normalizes_rows_from_before_normalization(self, tmp_path):
        db_path = str(tmp_path / "old.db")
        conn = reading.connect(db_path)
        conn.execute(
            "INSERT INTO annotation (id, user_id, client_modified, source, updated_at)"
            " VALUES ('a1', 1, '2026-10-17T05:42:47Z', 'sync', 'x')"
        )
        conn.commit()
        conn.close()
        conn = reading.connect(db_path)
        assert conn.execute("SELECT client_modified FROM annotation").fetchone()[0] == \
            "2026-10-17T05:42:47.000000+00:00"

    @pytest.mark.parametrize("value,expected", [
        ("2026-10-17T05:42:47Z", "2026-10-17T05:42:47.000000+00:00"),
        ("2026-10-17T05:42:47.806", "2026-10-17T05:42:47.806000+00:00"),
        ("2026-10-17T07:42:47+02:00", "2026-10-17T05:42:47.000000+00:00"),
        ("garbage", "garbage"),
        (None, None),
    ])
    def test_normalize_timestamp(self, value, expected):
        assert reading.normalize_timestamp(value) == expected

    def test_hidden_bookmarks_are_skipped(self, conn, tmp_path):
        _device_db(tmp_path / "KoboReader.sqlite", hidden=True)
        reading.ingest_device_upload(conn, 1, (tmp_path / "KoboReader.sqlite").read_bytes())
        assert "a1" not in _annotations(conn)

    @pytest.mark.parametrize("body", [b"not a database", gzip.compress(b"still not a database")])
    def test_non_sqlite_upload_is_rejected(self, conn, body):
        with pytest.raises(ValueError):
            reading.ingest_device_upload(conn, 1, body)


@pytest.mark.unit
class TestBackfill:
    def test_replays_captured_exchanges(self, conn, tmp_path):
        capture = _load("kobo_capture")
        capture_db = str(tmp_path / "capture.db")
        for method, path, body in [
            ("PATCH", f"/api/v3/content/{BOOK}/annotations",
             {"updatedAnnotations": [_annotation("a1", "2026-10-17T05:42:47Z")]}),
            ("PUT", f"/kobo/s3cret/v1/library/{BOOK}/state", _state("2026-10-17T05:35:02Z", 16)),
            ("POST", "/kobo/s3cret/v1/user/recommendations/feedback",
             {"FeedbackItems": [{"FeedbackType": "Rate4Star", "RevisionId": BOOK}]}),
            ("POST", "/kobo/s3cret/v1/analytics/gettests", {"TestKey": "x"}),
        ]:
            capture.record_exchange(
                capture_db, method=method, path=path, query="", status=200, request_headers={},
                request_body=json.dumps(body).encode(), response_headers={}, response_body=b"{}",
            )
        assert reading.backfill_from_capture(capture_db, conn, 1) == 3
        assert reading.backfill_from_capture(capture_db, conn, 1) == 3
        assert conn.execute("SELECT count(*) FROM annotation").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM reading_state").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM feedback").fetchone()[0] == 1


@pytest.mark.unit
class TestIngestHook:
    def _app(self, tmp_path, user_id):
        app = Flask(__name__)
        db_path = str(tmp_path / "reading.db")

        @app.route("/api/v3/content/<book>/annotations", methods=["PATCH"])
        def annotations(book):
            return jsonify({"data": [], "totalResults": 0})

        @app.route("/kobo/<token>/v1/library/<book>/state", methods=["PUT"])
        def state(token, book):
            raise RuntimeError("handler blew up")

        app.after_request(reading.make_ingest_hook(lambda: user_id, db_path))
        return app, db_path

    def test_hook_stores_annotations_without_changing_response(self, tmp_path):
        app, db_path = self._app(tmp_path, 1)
        resp = app.test_client().patch(
            f"/api/v3/content/{BOOK}/annotations",
            json={"updatedAnnotations": [_annotation("a1", "2026-10-17T05:42:47Z")]},
        )
        assert resp.get_json() == {"data": [], "totalResults": 0}
        assert reading.connect(db_path).execute("SELECT id FROM annotation").fetchone()[0] == "a1"

    def test_hook_skips_requests_it_cannot_attribute(self, tmp_path):
        app, db_path = self._app(tmp_path, None)
        app.test_client().patch(
            f"/api/v3/content/{BOOK}/annotations",
            json={"updatedAnnotations": [_annotation("a1", "2026-10-17T05:42:47Z")]},
        )
        assert not Path(db_path).exists()

    def test_hook_ignores_server_errors(self, tmp_path):
        app, db_path = self._app(tmp_path, 1)
        resp = app.test_client().put(f"/kobo/t/v1/library/{BOOK}/state", json=_state("2026-10-17T05:35:02Z", 16))
        assert resp.status_code == 500
        assert not Path(db_path).exists()
