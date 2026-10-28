"""Crema's Google account (tools/crema_google.py) against a fake Google: its sign-in, Gmail, Calendar and Drive."""
import base64
import json
import urllib.parse
from email import message_from_bytes, policy
from email.message import EmailMessage

import pytest

from tools import crema_google as google
from tools import crema_mail_tool as mail_tool



def id_token(email):
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"head.{payload}.sig"


TOKEN, USER = "ya29.live", "me@gmail.com"


def gmail_raw(subject, body):
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = "김철수 <kim@example.com>", USER, subject
    message["Message-ID"] = "<m1@example.com>"
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")


# Gmail's labels: its own folders, a category (not a folder), and one the user made.
LABELS = [{"id": "INBOX", "name": "INBOX", "type": "system"}, {"id": "SENT", "name": "SENT", "type": "system"},
          {"id": "TRASH", "name": "TRASH", "type": "system"}, {"id": "CATEGORY_SOCIAL", "name": "CATEGORY_SOCIAL", "type": "system"},
          {"id": "Label_1", "name": "정리", "type": "user"}]
SUMMARIES = {"m1": {"labelIds": ["INBOX", "UNREAD"], "payload": {"mimeType": "multipart/mixed", "headers": [
                 {"name": "From", "value": "김철수 <kim@example.com>"}, {"name": "Subject", "value": "=?UTF-8?B?6rKs7KCB7ISc?="}]}},
             "m2": {"labelIds": ["INBOX"], "payload": {"mimeType": "text/plain", "headers": [{"name": "Subject", "value": "회의"}]}}}


class FakeGoogle:
    """Google's sign-in and REST APIs: answers by method and address, and remembers what was asked."""

    def __init__(self):
        # What Google says was allowed and by whom; None: the feature asked for, by the user.
        self.calls, self.token_scope, self.allowed_by, self.refreshes = [], None, USER, 0

    def __call__(self, method, url, *, form=None, body=None, token="", content_type=""):
        self.calls.append((method, url, form, body))
        if url == google.REVOKE:
            return b""
        if url == google.TOKEN:
            if form["grant_type"] == "refresh_token":
                self.refreshes += 1
                return json.dumps({"access_token": TOKEN, "expires_in": 3600}).encode()
            asked = urllib.parse.parse_qs(urllib.parse.urlsplit(self.consent).query)["scope"][0]
            return json.dumps({"access_token": TOKEN, "refresh_token": "1//refresh", "expires_in": 3600,
                               "scope": self.token_scope if self.token_scope is not None else asked,
                               "id_token": id_token(self.allowed_by)}).encode()
        assert token == TOKEN
        if url.startswith(mail_tool.GMAIL):
            path = url[len(mail_tool.GMAIL):]
            if path == "/profile":
                return json.dumps({"emailAddress": USER}).encode()
            if path == "/labels":
                return json.dumps({"labels": LABELS}).encode()
            if path.startswith("/messages?"):
                return json.dumps({"messages": [{"id": "m1"}, {"id": "m2"}], "resultSizeEstimate": 2}).encode()
            if "format=metadata" in path:
                return json.dumps(SUMMARIES[path.split("/")[2].split("?")[0]]).encode()
            if path == "/messages/m1?format=raw":
                return json.dumps({"raw": gmail_raw("견적서", "견적서를 보내드립니다."), "labelIds": ["INBOX", "UNREAD"]}).encode()
            if method == "POST":
                return b"{}"
        if url.endswith("/users/me/calendarList/primary"):
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
    global FAKE
    found = FAKE = FakeGoogle()
    monkeypatch.setattr(google, "_http", found)
    # Windows DPAPI stands in as a reversible wrapper (these tests run on Linux).
    monkeypatch.setattr(google, "_seal", lambda text: google.SEALED + text[::-1])
    monkeypatch.setattr(google, "_unseal", lambda value: value[len(google.SEALED):][::-1])
    google._tokens.clear()
    asked = []
    monkeypatch.setattr(mail_tool, "ask_to_send", lambda summary, what="메일 보내기", key="mail_send": asked.append((what, summary)) or None)
    return found, asked


FAKE = None


def sign_in(feature="mail", address="me@gmail.com", **back):
    started = google.start(feature, address, "http://127.0.0.1:5555/")
    FAKE.consent = started["url"]
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(started["url"]).query))
    how = google.finish(back.get("code", "code-1"), back.get("state", query["state"]), back.get("error", ""))
    return started, query, how


def test_each_feature_asks_only_its_permission_and_the_account_keeps_what_it_has(fake):
    from hermes_cli.config import get_env_value

    started, query, how = sign_in()
    assert query["redirect_uri"] == "http://127.0.0.1:5555" + google.CALLBACK
    assert query["scope"] == "https://www.googleapis.com/auth/gmail.modify openid email" and query["include_granted_scopes"] == "true"
    assert query["access_type"] == "offline" and query["login_hint"] == "me@gmail.com" and query["code_challenge_method"] == "S256"
    assert query["client_id"] == google.CLIENT_ID
    assert how == "done" and google.status(started["flow"]) == {"status": "done", "address": "me@gmail.com", "feature": "mail"}
    assert mail_tool.accounts() == [{"address": "me@gmail.com", "provider": "google"}]
    assert google.state() == {"accounts": [{"address": "me@gmail.com", "features": ["mail"]}]}
    kept = get_env_value("CREMA_GOOGLE_ME_GMAIL_COM")
    assert kept.startswith(google.SEALED) and "1//refresh" not in kept
    assert google._grant("me@gmail.com") == {"refresh_token": "1//refresh"}
    assert not get_env_value("CREMA_MAIL_PASSWORD_ME_GMAIL_COM")
    # A second coming back with the same state finds nothing to settle.
    assert google.finish("code-1", query["state"], "") == "unknown"
    # Calendar next: no address given, the account kept is used; Gmail is not a calendar.
    assert not google.connected("calendar")
    _, query, how = sign_in("calendar", "")
    assert how == "done" and query["login_hint"] == "me@gmail.com"
    assert query["scope"] == f"{google.FEATURES['calendar']} openid email"
    assert google.accounts() == [{"address": "me@gmail.com", "features": ["calendar", "mail"]}]
    # Gmail taken away from the mail list: Calendar stays, with its sign-in.
    mail_tool.remove("me@gmail.com")
    assert mail_tool.accounts() == [] and google.accounts() == [{"address": "me@gmail.com", "features": ["calendar"]}]
    assert get_env_value("CREMA_GOOGLE_ME_GMAIL_COM")
    assert not [c for c in FAKE.calls if c[1] == google.REVOKE]
    # The last feature gone: Google takes the sign-in back, then it leaves this PC.
    google.remove("calendar", "me@gmail.com")
    assert google.accounts() == [] and not get_env_value("CREMA_GOOGLE_ME_GMAIL_COM")
    assert [c[2] for c in FAKE.calls if c[1] == google.REVOKE] == [{"token": "1//refresh"}]


def test_drive_alone_takes_the_account_that_allowed_it(fake):
    found, _ = fake
    found.allowed_by = "work@gmail.com"
    _, query, how = sign_in("drive", "")
    assert how == "done" and "login_hint" not in query
    assert google.accounts() == [{"address": "work@gmail.com", "features": ["drive"]}] and mail_tool.accounts() == []



def test_a_permission_left_unchecked_or_another_account_is_not_kept(fake):
    found, _ = fake
    found.token_scope = "openid email"
    started, _, how = sign_in()
    assert how == "scopes" and google.status(started["flow"])["reason"] == "scopes"
    # Calendar asks two permissions: one of them left unchecked is not enough.
    found.token_scope = "https://www.googleapis.com/auth/calendar.events openid email"
    _, _, how = sign_in("calendar", "")
    assert how == "scopes"
    found.token_scope = None
    found.allowed_by = "other@gmail.com"
    _, _, how = sign_in()
    assert how == "account"
    found.allowed_by = USER
    _, _, how = sign_in(error="access_denied")
    assert how == "denied"
    assert mail_tool.accounts() == [] and google.accounts() == []
    # An app password cannot connect Gmail: it goes through Google's sign-in.
    assert mail_tool.connect("me@gmail.com", "abcdabcdabcdabcd") == {"ok": False, "reason": "unknown"}
def test_gmail_is_the_mail_tool_over_gmails_api(fake):
    found, asked = fake
    sign_in()
    google._tokens.clear()

    def call(**args):
        return json.loads(mail_tool.mail(args))

    def posted(path):
        return [json.loads(c[3]) for c in found.calls if c[0] == "POST" and c[1] == mail_tool.GMAIL + path]

    # Its labels are the folders: Gmail's own by their role, the user's by name; categories are not folders.
    assert call(action="folders")["folders"] == [{"name": "INBOX", "role": "inbox"}, {"name": "SENT", "role": "sent"},
                                                  {"name": "TRASH", "role": "trash"}, {"name": "정리"}]
    found_mail = call(action="search", query="견적", sender="kim", since="2026-10-20", unread_only=True)
    listed = next(c[1] for c in found.calls if "/messages?" in c[1])
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(listed).query))
    assert query["labelIds"] == "INBOX" and query["q"] == 'is:unread after:2026/10/20 from:"kim" 견적'
    assert found_mail["mails"][0] == {"uid": "m1", "date": "", "from": "김철수 <kim@example.com>", "to": "",
                                      "subject": "견적서", "unread": True, "attachments": True}
    assert call(action="read", uid="m1")["body"] == "견적서를 보내드립니다."
    # Sorting: into a label (out of the inbox), to the trash (never deleted for good), marked read.
    assert call(action="move", uid="m2", to_folder="정리") == {"moved": "m2", "from": "INBOX", "to": "정리"}
    assert posted("/messages/m2/modify") == [{"addLabelIds": ["Label_1"], "removeLabelIds": ["INBOX"]}]
    assert call(action="move", uid="m1", to_folder="trash")["to"] == "TRASH" and posted("/messages/m1/trash") == [{}]
    call(action="mark", uid="m1", mark="read")
    assert posted("/messages/m1/modify") == [{"removeLabelIds": ["UNREAD"]}]
    # A reply goes after the approval card, through Gmail, which keeps its own sent copy.
    result = call(action="send", reply_to_uid="m1", bcc=["boss@example.com"], body="잘 받았습니다.")
    assert result["sent"] is True and "kept_in" not in result and asked[-1][0] == "메일 보내기"
    raw = posted("/messages/send")[0]["raw"]
    sent = message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)
    assert (sent["From"], sent["To"], sent["Bcc"], sent["Subject"]) == (USER, "kim@example.com", "boss@example.com", "Re: 견적서")
    assert sent["In-Reply-To"] == "<m1@example.com>"
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
