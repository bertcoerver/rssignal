"""Tests for rssignal.server (signal-cli and runs are faked; HTTP is real, on localhost)."""

import base64
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from rssignal import server
from rssignal.server import (
    SendRefused,
    Service,
    decode_attachment,
    next_slot,
    parse_schedule,
    resolve_recipient,
)
from rssignal.signal_cli import SignalGroup

GROUPS = [
    SignalGroup(id="AAAA1111+bbbb/cc=", name="Laundry"),
    SignalGroup(id="LEFT=", name="Old", active=False),
]
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20


# --- recipients -------------------------------------------------------------


@pytest.mark.parametrize(
    "to, expected",
    [
        ("+31600000000", "+31600000000"),
        ("group:AAAA1111+bbbb/cc=", "group:AAAA1111+bbbb/cc="),
        ("laundry", "group:AAAA1111+bbbb/cc="),
        ("  Laundry ", "group:AAAA1111+bbbb/cc="),
        # signal-cli-rest-api's spelling: the id, base64'd once more.
        ("group." + base64.b64encode(b"AAAA1111+bbbb/cc=").decode(), "group:AAAA1111+bbbb/cc="),
        # ...or somebody pasted the plain id after "group.".
        ("group.AAAA1111+bbbb/cc=", "group:AAAA1111+bbbb/cc="),
    ],
)
def test_resolve_recipient(to, expected):
    assert resolve_recipient(to, GROUPS) == expected


def test_resolve_recipient_refuses_a_group_it_does_not_have():
    with pytest.raises(SendRefused, match="No group called 'Dishwasher'"):
        resolve_recipient("Dishwasher", GROUPS)


def test_resolve_recipient_never_picks_a_group_that_was_left():
    with pytest.raises(SendRefused):
        resolve_recipient("Old", GROUPS)


# --- attachments ------------------------------------------------------------


def test_decode_attachment_from_bare_base64_guesses_the_type():
    data, ext = decode_attachment(base64.b64encode(PNG).decode(), 0)
    assert data == PNG and ext == ".png"


def test_decode_attachment_from_a_data_uri_takes_its_filename():
    value = "data:image/jpeg;filename=door.jpeg;base64," + base64.b64encode(b"\xff\xd8\xffxx").decode()
    assert decode_attachment(value, 0) == (b"\xff\xd8\xffxx", ".jpeg")


def test_decode_attachment_refuses_nothing():
    with pytest.raises(SendRefused, match="empty"):
        decode_attachment("", 0)


# --- the schedule -----------------------------------------------------------


def test_parse_schedule_sorts_and_refuses_nonsense():
    assert parse_schedule("15:00, 01:30,06:30") == [(1, 30), (6, 30), (15, 0)]
    assert parse_schedule("") == []
    with pytest.raises(ValueError, match="25:00"):
        parse_schedule("25:00")


def test_next_slot_is_today_or_tomorrow():
    tz = timezone(timedelta(hours=1))
    slots = [(1, 30), (15, 0)]
    assert next_slot(slots, datetime(2026, 11, 7, 9, 0, tzinfo=tz)) == datetime(2026, 11, 7, 15, 0, tzinfo=tz)
    assert next_slot(slots, datetime(2026, 11, 7, 15, 0, tzinfo=tz)) == datetime(2026, 11, 8, 1, 30, tzinfo=tz)
    assert next_slot([], datetime(2026, 11, 7, tzinfo=tz)) is None


# --- over HTTP --------------------------------------------------------------


@pytest.fixture
def running(monkeypatch, tmp_path):
    """A real server on a free localhost port, with signal-cli faked out."""
    monkeypatch.setenv("RSSIGNAL_ACCOUNT", "+31600000000")
    sent = []

    def fake_send(text, recipient=None, attachments=None, timeout=None, **kwargs):
        files = [open(path, "rb").read() for path in attachments or []]
        sent.append({"text": text, "recipient": recipient, "files": files})

    monkeypatch.setattr(server, "send_msg", fake_send)
    monkeypatch.setattr(server, "list_groups", lambda: list(GROUPS))
    monkeypatch.setattr(server, "list_accounts", lambda: ["+31600000000"])
    config = tmp_path / "feeds.json"
    config.write_text(json.dumps({"feeds": [{"name": "Laundry"}]}))

    def start(**kwargs):
        service = Service(str(config), **kwargs)
        httpd = server.make_server(service, "127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        service.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        started.append((httpd, service))
        return base, service

    started = []
    yield start, sent
    for httpd, service in started:
        httpd.shutdown()
        httpd.server_close()
        service.stop()


def _call(base, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(base + path, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_home_assistants_signal_messenger_can_send(running):
    """What pysignalclirestapi does: GET /v1/about, then POST /v2/send, expecting 201."""
    start, sent = running
    base, _ = start()

    status, about = _call(base, "/v1/about")
    assert status == 200 and "v2" in about["versions"] and "mode" in about

    status, reply = _call(base, "/v2/send", {
        "message": "The washing machine is done.",
        "number": "+31600000000",
        "recipients": ["Laundry", "+31611111111"],
        "notify_self": True,
        "text_mode": "normal",
        "base64_attachments": [base64.b64encode(PNG).decode()],
    })
    assert status == 201 and "timestamp" in reply
    assert [(s["recipient"], s["text"]) for s in sent] == [
        ("group:AAAA1111+bbbb/cc=", "The washing machine is done."),
        ("+31611111111", "The washing machine is done."),
    ]
    assert sent[0]["files"] == [PNG]


def test_an_unknown_group_is_an_error_the_integration_can_show(running):
    start, sent = running
    base, _ = start()
    status, reply = _call(base, "/v2/send", {"message": "x", "recipients": ["Dishwasher"]})
    assert status == 400 and "No group called 'Dishwasher'" in reply["error"]
    assert sent == []


def test_api_send_takes_a_name_or_a_list(running):
    start, sent = running
    base, service = start()
    assert _call(base, "/api/send", {"message": "hi", "to": "Laundry"})[0] == 201
    assert _call(base, "/api/send", {"message": "hi", "to": ["+31611111111"]})[0] == 201
    assert len(sent) == 2
    assert service.activity[0]["kind"] == "send"


def test_sending_needs_an_allowed_address_or_the_token(running):
    start, sent = running
    base, service = start(gui_from="10.9.9.9", api_from="10.9.9.9", api_token="s3cret")

    status, reply = _call(base, "/api/send", {"message": "hi", "to": "Laundry"})
    assert status == 403 and "not open to 127.0.0.1" in reply["error"]
    assert _call(base, "/v1/about")[0] == 403

    headers = {"Authorization": "Bearer s3cret"}
    assert _call(base, "/api/send", {"message": "hi", "to": "Laundry"}, headers)[0] == 201
    assert len(sent) == 1
    assert service.activity[-1]["kind"] == "refused"


def test_the_gui_is_only_for_its_own_addresses(running):
    start, _ = running
    base, _ = start(gui_from="10.9.9.9", api_from="127.0.0.1")
    assert _call(base, "/api/status")[0] == 403
    # ...while sending from an API address still works.
    assert _call(base, "/api/send", {"message": "hi", "to": "Laundry"})[0] == 201


def test_the_gui_page_and_status(running):
    start, _ = running
    base, _ = start(schedule=[(6, 30)])
    with urlopen(base + "/", timeout=10) as response:
        page = response.read().decode()
    assert "<title>rssignal</title>" in page and 'fetch(path' in page
    status, info = _call(base, "/api/status")
    assert status == 200
    assert info["account"] == "+31600000000" and info["linked"] is True
    assert info["schedule"] == ["06:30"] and info["next_run"]


def test_a_run_from_the_gui_reports_what_it_printed(running, monkeypatch):
    start, _ = running

    def fake_run(config, *, dry_run=False):
        print("[Laundry] -> group:AAAA1111+bbbb/cc=: done")
        return 1

    monkeypatch.setattr(server, "run_feeds", fake_run)
    base, _ = start()
    status, job = _call(base, "/api/run", {"dry_run": True})
    assert status == 202
    for _ in range(100):
        status, job = _call(base, f"/api/jobs/{job['id']}")
        if not job["running"]:
            break
        time.sleep(0.05)
    assert job["ok"] is True
    assert job["result"] == "1 item(s) would be sent."
    assert "[Laundry] -> group:AAAA1111+bbbb/cc=: done" in job["output"]


def test_feeds_overview_reads_the_library_and_the_groups(running, own_library):
    start, _ = running
    folder = own_library / "articles" / "Laundry"
    folder.mkdir(parents=True)
    (folder / "feed.json").write_text(json.dumps({"format": 1, "name": "Laundry"}))
    (folder / "a.json").write_text(json.dumps({"format": 1, "id": "a", "title": "Done", "kind": "text",
                                               "published": "2026-11-07T08:00:00+00:00"}))
    base, _ = start()
    status, reply = _call(base, "/api/feeds")
    assert status == 200
    (row,) = reply["feeds"]
    assert row["group"] == "group:AAAA1111+bbbb/cc="
    assert row["items"] == 1 and row["waiting"] == 1 and row["newest"] == "Done"
