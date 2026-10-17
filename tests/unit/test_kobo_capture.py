# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Unit tests for Kobo traffic capture."""

import importlib.util
import sqlite3
from pathlib import Path

import pytest
from flask import Flask, jsonify, request


def _load_capture_module():
    module_path = Path(__file__).resolve().parents[2] / "cps" / "kobo_capture.py"
    spec = importlib.util.spec_from_file_location("kobo_capture", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


kobo_capture = _load_capture_module()


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM exchange ORDER BY id")]
    finally:
        conn.close()


@pytest.mark.unit
class TestCapturePaths:
    @pytest.mark.parametrize("path", [
        "/kobo/abc123/v1/analytics/event",
        "/kobo/abc123/v1/library/sync",
        "/kobo/abc123/v1/library/0f1e/state",
        "/api/v3/content/0f1e/annotations",
        "/api/v3/content/checkforchanges",
        "/api/UserStorage/Metadata",
        "/api/internal/notebooks",
    ])
    def test_device_paths_are_captured(self, path):
        assert kobo_capture.is_capture_path(path)

    @pytest.mark.parametrize("path", [
        "/kobo/abc123/download/12/kepub",
        "/kobo/abc123/0f1e/150/200/false/image.jpg",
        "/login",
        "/api/books",
        "/kosync/users/auth",
    ])
    def test_other_paths_are_ignored(self, path):
        assert not kobo_capture.is_capture_path(path)

    def test_redact_path_hides_auth_token(self):
        assert kobo_capture.redact_path("/kobo/s3cret/v1/library/sync") == "/kobo/<token>/v1/library/sync"
        assert kobo_capture.redact_path("/api/UserStorage/Metadata") == "/api/UserStorage/Metadata"

    def test_select_headers_drops_credentials_keeps_protocol_state(self):
        kept = kobo_capture.select_headers({
            "Authorization": "Bearer x",
            "x-kobo-userkey": "account",
            "Cookie": "session=1",
            "x-kobo-synctoken": "state",
            "User-Agent": "Kobo Touch",
            "Accept-Language": "en",
        })
        assert kept == {"x-kobo-synctoken": "state", "User-Agent": "Kobo Touch"}

    @pytest.mark.parametrize("value,expected", [
        ("true", True), ("1", True), (" Yes ", True), ("", False), ("false", False),
    ])
    def test_capture_enabled_reads_env(self, value, expected):
        assert kobo_capture.capture_enabled({"CWA_KOBO_CAPTURE": value}) is expected


@pytest.mark.unit
class TestRecordExchange:
    def test_bodies_are_truncated_but_sizes_kept(self, tmp_path, monkeypatch):
        db_path = tmp_path / "capture.db"
        monkeypatch.setattr(kobo_capture, "MAX_RESPONSE_BODY", 4)
        kobo_capture.record_exchange(
            str(db_path), method="GET", path="/kobo/t/v1/library/sync", query="",
            status=200, request_headers={}, request_body=None,
            response_headers={}, response_body=b"0123456789",
        )
        (row,) = _rows(db_path)
        assert row["response_body"] == b"0123"
        assert row["response_size"] == 10
        assert row["request_size"] == 0
        assert row["query"] is None


@pytest.mark.unit
class TestAfterRequestHook:
    @pytest.fixture
    def app(self, tmp_path):
        app = Flask(__name__)
        app.config["db_path"] = str(tmp_path / "capture.db")

        @app.route("/kobo/<token>/v1/analytics/event", methods=["POST"])
        def analytics(token):
            # Reading the body first mirrors real handlers; the hook must still see it.
            request.get_json()
            return jsonify({"Result": "Success"})

        @app.route("/login")
        def login():
            return "ok"

        app.after_request(kobo_capture.make_after_request_hook(app.config["db_path"]))
        return app

    def test_records_device_exchange(self, app):
        client = app.test_client()
        resp = client.post(
            "/kobo/s3cret/v1/analytics/event?x=1",
            json={"Events": [{"EventType": "LeaveContent"}]},
            headers={"Authorization": "Bearer x", "x-kobo-apiversion": "1"},
        )
        assert resp.status_code == 200
        (row,) = _rows(app.config["db_path"])
        assert row["method"] == "POST"
        assert row["path"] == "/kobo/<token>/v1/analytics/event"
        assert row["query"] == "x=1"
        assert b"LeaveContent" in row["request_body"]
        assert b"Success" in row["response_body"]
        assert "Authorization" not in row["request_headers"]
        assert "x-kobo-apiversion: 1" in row["request_headers"].lower()

    def test_records_responses_short_circuited_by_before_request(self, app):
        # CWA's reading-services stub answers annotation PATCHes from a
        # before_app_request hook; those payloads are the ones worth keeping.
        @app.before_request
        def stub():
            if request.path.endswith("/annotations"):
                return jsonify({"data": [], "totalResults": 0})
            return None

        app.test_client().patch(
            "/api/v3/content/0f1e/annotations",
            json={"updatedAnnotations": [{"id": "a1", "highlightedText": "prescience"}]},
        )
        (row,) = _rows(app.config["db_path"])
        assert row["status"] == 200
        assert b"prescience" in row["request_body"]
        assert b"totalResults" in row["response_body"]

    def test_ignores_non_kobo_requests(self, app):
        app.test_client().get("/login")
        assert not Path(app.config["db_path"]).exists()

    def test_capture_failure_does_not_break_response(self, app, monkeypatch):
        def boom(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(kobo_capture, "record_exchange", boom)
        resp = app.test_client().post("/kobo/t/v1/analytics/event", json={})
        assert resp.status_code == 200
        assert resp.get_json() == {"Result": "Success"}
