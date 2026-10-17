"""Crema's Google account (tools/crema_google.py) against a fake Google and a fake Gmail IMAP and SMTP."""
import base64
import imaplib
import json
import smtplib
import urllib.parse

import pytest

from tools import crema_google as google
from tools import crema_mail_tool as mail_tool



def id_token(email):
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"head.{payload}.sig"


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
        # What Google says was allowed and by whom; None: the feature asked for, by the user.
        self.calls, self.token_scope, self.allowed_by, self.refreshes = [], None, FakeGmail.user, 0

    def __call__(self, method, url, *, form=None, body=None, token="", content_type=""):
        self.calls.append((method, url, form, body))
        if url == google.TOKEN:
            if form["grant_type"] == "refresh_token":
                self.refreshes += 1
                return json.dumps({"access_token": FakeGmail.token, "expires_in": 3600}).encode()
            asked = urllib.parse.parse_qs(urllib.parse.urlsplit(self.consent).query)["scope"][0]
            return json.dumps({"access_token": FakeGmail.token, "refresh_token": "1//refresh", "expires_in": 3600,
                               "scope": self.token_scope if self.token_scope is not None else asked,
                               "id_token": id_token(self.allowed_by)}).encode()
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
    global FAKE
    found = FAKE = FakeGoogle()
    monkeypatch.setattr(google, "_http", found)
    google._tokens.clear()
    asked = []
    monkeypatch.setattr(mail_tool, "ask_to_send", lambda summary, what="메일 보내기", key="mail_send": asked.append((what, summary)) or None)
    return found, asked


FAKE = None


def sign_in(feature="mail", address="me@gmail.com", client=("123-abc.apps.googleusercontent.com", "GOCSPX-secret"), **back):
    started = google.start(feature, address, *client, "http://127.0.0.1:5555/")
    FAKE.consent = started["url"]
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(started["url"]).query))
    how = google.finish(back.get("code", "code-1"), back.get("state", query["state"]), back.get("error", ""))
    return started, query, how


def test_each_feature_asks_only_its_permission_and_the_account_keeps_what_it_has(fake):
    from hermes_cli.config import get_env_value

    started, query, how = sign_in()
    assert query["redirect_uri"] == "http://127.0.0.1:5555" + google.CALLBACK
    assert query["scope"] == "https://mail.google.com/ openid email" and query["include_granted_scopes"] == "true"
    assert query["access_type"] == "offline" and query["login_hint"] == "me@gmail.com" and query["code_challenge_method"] == "S256"
    assert how == "done" and google.status(started["flow"]) == {"status": "done", "address": "me@gmail.com", "feature": "mail"}
    assert mail_tool.accounts() == [{"address": "me@gmail.com", "provider": "google"}]
    assert google.state() == {"accounts": [{"address": "me@gmail.com", "features": ["mail"]}], "client": True, "project": "123"}
    kept = json.loads(get_env_value("CREMA_GOOGLE_ME_GMAIL_COM"))
    assert kept == {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-secret", "refresh_token": "1//refresh"}
    assert not get_env_value("CREMA_MAIL_PASSWORD_ME_GMAIL_COM")
    # A second coming back with the same state finds nothing to settle.
    assert google.finish("code-1", query["state"], "") == "unknown"
    # Calendar next: no address and no app given, the account and its app kept are used; Gmail is not a calendar.
    assert not google.connected("calendar")
    _, query, how = sign_in("calendar", "", ("", ""))
    assert how == "done" and query["client_id"] == "123-abc.apps.googleusercontent.com" and query["login_hint"] == "me@gmail.com"
    assert query["scope"] == "https://www.googleapis.com/auth/calendar openid email"
    assert google.accounts() == [{"address": "me@gmail.com", "features": ["calendar", "mail"]}]
    # Gmail taken away from the mail list: Calendar stays, with its sign-in.
    mail_tool.remove("me@gmail.com")
    assert mail_tool.accounts() == [] and google.accounts() == [{"address": "me@gmail.com", "features": ["calendar"]}]
    assert get_env_value("CREMA_GOOGLE_ME_GMAIL_COM")
    google.remove("calendar", "me@gmail.com")
    assert google.accounts() == [] and not get_env_value("CREMA_GOOGLE_ME_GMAIL_COM")


def test_drive_alone_with_a_new_app_takes_the_account_that_allowed_it(fake):
    found, _ = fake
    found.allowed_by = "work@gmail.com"
    _, query, how = sign_in("drive", "")
    assert how == "done" and "login_hint" not in query
    assert google.accounts() == [{"address": "work@gmail.com", "features": ["drive"]}] and mail_tool.accounts() == []
    # Without an app given and none kept, there is nothing to sign in with.
    google.remove("drive", "work@gmail.com")
    with pytest.raises(ValueError):
        google.start("calendar", "", "", "", "http://127.0.0.1:5555/")


def test_a_permission_left_unchecked_or_another_account_is_not_kept(fake):
    found, _ = fake
    found.token_scope = "openid email"
    started, _, how = sign_in()
    assert how == "scopes" and google.status(started["flow"])["reason"] == "scopes"
    found.token_scope = None
    found.allowed_by = "other@gmail.com"
    _, _, how = sign_in()
    assert how == "account"
    found.allowed_by = FakeGmail.user
    _, _, how = sign_in(error="access_denied")
    assert how == "denied"
    assert mail_tool.accounts() == [] and google.accounts() == []
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
    sign_in("calendar")
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
    # Drive is not connected: its tool says so.
    assert "Google Drive is not connected" in google.drive({"action": "search"})
    google.remove("calendar", "me@gmail.com")


def test_drive_reads_a_google_doc_as_text(fake):
    sign_in("drive")
    assert json.loads(google.drive({"action": "read", "file_id": "doc1"})) == {"name": "회의록", "type": "application/vnd.google-apps.document", "text": "1. 예산 확정"}
    assert "unknown action" in google.drive({"action": "nope", "file_id": "doc1"})
    google.remove("drive", "me@gmail.com")
