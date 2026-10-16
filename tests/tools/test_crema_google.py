"""Crema's Google account (tools/crema_google.py) against a fake Google and a fake Gmail IMAP and SMTP."""
import imaplib
import json
import smtplib
import urllib.parse

import pytest

from tools import crema_google as google
from tools import crema_mail_tool as mail_tool

ALL = " ".join(google.SCOPES)


class FakeGmail:
    """IMAP and SMTP at once: lets in only `token` for `user`."""
    token, user, sent = "ya29.live", "me@gmail.com", []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.capabilities = ("IMAP4REV1",)

    def authenticate(self, mechanism, answer):
        if answer(b"") != f"user={self.user}\x01auth=Bearer {self.token}\x01\x01".encode():
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b""]

    def auth(self, mechanism, answer, initial_response_ok=True):
        assert mechanism == "XOAUTH2" and answer() == f"user={self.user}\x01auth=Bearer {self.token}\x01\x01"

    def send_message(self, message, from_addr=None, to_addrs=None):
        FakeGmail.sent.append(message)

    def logout(self):
        return "BYE", [b""]

    def quit(self):
        pass


class FakeGoogle:
    """Google's sign-in and REST APIs: answers by method and address, and remembers what was asked."""

    def __init__(self):
        self.calls, self.token_scope, self.refreshes = [], ALL, 0

    def __call__(self, method, url, *, form=None, body=None, token="", content_type=""):
        self.calls.append((method, url, form, body))
        if url == google.TOKEN:
            if form["grant_type"] == "refresh_token":
                self.refreshes += 1
                return json.dumps({"access_token": FakeGmail.token, "expires_in": 3600}).encode()
            return json.dumps({"access_token": FakeGmail.token, "refresh_token": "1//refresh", "scope": self.token_scope,
                               "expires_in": 3600}).encode()
        assert token == FakeGmail.token
        if url.endswith("/calendars/primary"):
            return json.dumps({"timeZone": "Asia/Seoul"}).encode()
        if "/events/ev1" in url and method == "GET":
            return json.dumps({"id": "ev1", "summary": "주간 회의", "attendees": [{"email": "me@gmail.com", "self": True},
                                                                              {"email": "kim@example.com"}]}).encode()
        if "/events" in url and method in ("POST", "PATCH"):
            return json.dumps({"id": "ev2", **json.loads(body)}).encode()
        if method == "DELETE":
            return b""
        if url.startswith(f"{google.DRIVE}/files/doc1?fields"):
            return json.dumps({"id": "doc1", "name": "회의록", "mimeType": "application/vnd.google-apps.document"}).encode()
        if url.startswith(f"{google.DRIVE}/files/doc1/export"):
            return "1. 예산 확정".encode()
        raise AssertionError((method, url))


@pytest.fixture
def fake(monkeypatch):
    FakeGmail.sent = []
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeGmail)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeGmail)
    found = FakeGoogle()
    monkeypatch.setattr(google, "_http", found)
    google._tokens.clear()
    asked = []
    monkeypatch.setattr(mail_tool, "ask_to_send", lambda summary, what="메일 보내기", key="mail_send": asked.append((what, summary)) or None)
    return found, asked


def sign_in(address="me@gmail.com", **back):
    started = google.start(address, "123-abc.apps.googleusercontent.com", "GOCSPX-secret", "http://127.0.0.1:5555/")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(started["url"]).query))
    how = google.finish(back.get("code", "code-1"), back.get("state", query["state"]), back.get("error", ""))
    return started, query, how


def test_the_users_own_app_signs_crema_in_and_the_account_is_kept(fake):
    from hermes_cli.config import get_env_value

    started, query, how = sign_in()
    assert query["redirect_uri"] == "http://127.0.0.1:5555" + google.CALLBACK
    assert query["scope"] == ALL and query["access_type"] == "offline" and query["login_hint"] == "me@gmail.com"
    assert query["code_challenge_method"] == "S256"
    assert how == "done" and google.status(started["flow"]) == {"status": "done", "address": "me@gmail.com"}
    assert mail_tool.accounts() == [{"address": "me@gmail.com", "provider": "google"}]
    kept = json.loads(get_env_value("CREMA_MAIL_PASSWORD_ME_GMAIL_COM"))
    assert kept == {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-secret", "refresh_token": "1//refresh"}
    # A second coming back with the same state finds nothing to settle.
    assert google.finish("code-1", query["state"], "") == "unknown"
    mail_tool.remove("me@gmail.com")


def test_a_permission_left_unchecked_or_another_account_is_not_kept(fake):
    found, _ = fake
    found.token_scope = "https://mail.google.com/"
    started, _, how = sign_in()
    assert how == "scopes" and google.status(started["flow"])["reason"] == "scopes"
    found.token_scope = ALL
    _, _, how = sign_in("other@gmail.com")
    assert how == "account"
    _, _, how = sign_in(error="access_denied")
    assert how == "denied"
    assert mail_tool.accounts() == []
    # An app password cannot connect Gmail: it goes through Google's sign-in.
    assert mail_tool.connect("me@gmail.com", "abcdabcdabcdabcd") == {"ok": False, "reason": "unknown"}


def test_gmail_is_the_mail_tool_with_googles_sign_in_and_keeps_its_own_sent_copy(fake):
    found, asked = fake
    sign_in()
    google._tokens.clear()
    result = json.loads(mail_tool.mail({"action": "send", "to": ["kim@example.com"], "subject": "안녕", "body": "본문"}))
    assert result["sent"] is True and "kept_in" not in result
    assert FakeGmail.sent[-1]["From"] == "me@gmail.com" and asked[-1][0] == "메일 보내기"
    # The access token was refreshed once and reused.
    assert found.refreshes == 1
    mail_tool.remove("me@gmail.com")


def test_a_calendar_change_that_tells_others_is_asked_first(fake, monkeypatch):
    found, asked = fake
    sign_in()
    made = json.loads(google.calendar({"action": "create", "summary": "점심", "start": "2026-10-20T12:00", "end": "2026-10-20T13:00"}))
    assert made["created"] == {"id": "ev2", "summary": "점심", "start": "2026-10-20T12:00:00", "end": "2026-10-20T13:00:00"}
    post = next(call for call in found.calls if call[0] == "POST" and "/events" in call[1])
    assert "sendUpdates=none" in post[1] and json.loads(post[3])["start"] == {"dateTime": "2026-10-20T12:00:00", "timeZone": "Asia/Seoul"}
    assert asked == []
    # Deleting an event with an attendee tells them: asked, and declined, nothing is deleted.
    monkeypatch.setattr(mail_tool, "ask_to_send", lambda summary, what, key: asked.append((what, summary, key)) or "not sent: the user declined.")
    result = json.loads(google.calendar({"action": "delete", "event_id": "ev1"}))
    assert result["done"] is False and asked[-1] == ("일정 취소 알리기", "일정: 주간 회의 · 받는 사람: kim@example.com", "calendar_invite")
    assert not any(call[0] == "DELETE" for call in found.calls)
    mail_tool.remove("me@gmail.com")


def test_drive_reads_a_google_doc_as_text(fake):
    sign_in()
    assert json.loads(google.drive({"action": "read", "file_id": "doc1"})) == {"name": "회의록", "type": "application/vnd.google-apps.document", "text": "1. 예산 확정"}
    assert "unknown action" in google.drive({"action": "nope", "file_id": "doc1"})
    mail_tool.remove("me@gmail.com")
