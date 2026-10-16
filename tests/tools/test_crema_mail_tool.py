"""Crema mail (tools/crema_mail_tool.py) against a fake IMAP and SMTP server."""
import email
import imaplib
import json
import smtplib
from email.header import Header
from email.message import EmailMessage

import pytest

from tools import crema_mail_tool as mail_tool

PASSWORD = "abcd efgh ijkl"


def korean_mail(uid_subject, body, sender="김철수 <kim@example.com>", charset="euc-kr"):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "me@naver.com"
    message["Subject"] = Header(uid_subject, "utf-8").encode()
    message["Message-ID"] = "<m1@example.com>"
    message["Date"] = "Fri, 16 Oct 2026 09:00:00 +0900"
    message.set_content(body, charset=charset)
    return message.as_bytes()


class FakeIMAP:
    instances = []
    password = "abcdefghijkl"

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.capabilities = ("IMAP4REV1", "MOVE")
        self.literal = None
        self.selected = None
        self.searches = []
        self.appended = []
        self.stored = []
        self.mails = {"INBOX": {"7": (korean_mail("견적서 보내드립니다", "안녕하세요. 견적서를 첨부합니다."), ["\\Seen"]),
                                "9": (korean_mail("회의 일정", "내일 3시 회의입니다.", sender="boss@example.com"), [])}}
        FakeIMAP.instances.append(self)

    def login(self, user, password):
        if password != self.password:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"done"]

    def logout(self):
        return "BYE", [b""]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                      b'(\\HasNoChildren \\Sent) "/" "&vPSwuLpUx3zVaA-"',
                      b'(\\HasNoChildren) "/" "&1zTJwNG1-"',
                      b'(\\Noselect \\HasChildren) "/" "[Gmail]"']

    def select(self, name, readonly=False):
        self.selected = name
        return "OK", [b"2"]

    def append(self, folder, flags, date, data):
        self.appended.append((folder, flags, data))
        return "OK", [b"APPEND completed"]

    def expunge(self):
        return "OK", [b""]

    def uid(self, command, *args):
        box = self.mails["INBOX"]
        if command == "SEARCH":
            self.searches.append((args, self.literal))
            self.literal = None
            return "OK", [" ".join(sorted(box, key=int)).encode()]
        if command == "FETCH":
            out = []
            for uid in args[0].split(","):
                raw, flags = box[uid]
                head = f"{uid} (UID {uid} FLAGS ({' '.join(flags)}) BODY[] {{{len(raw)}}}".encode()
                part = raw.split(b"\n\n")[0] + b"\n\n" if "HEADER.FIELDS" in args[1] else raw
                out += [(head, part), b")"]
            return "OK", out
        if command in ("STORE", "MOVE", "COPY"):
            self.stored.append((command, args))
            return "OK", [b""]
        raise AssertionError(command)


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host = host

    def login(self, user, password):
        if password != FakeIMAP.password:
            raise smtplib.SMTPAuthenticationError(535, b"auth failed")

    def send_message(self, message, from_addr=None, to_addrs=None):
        FakeSMTP.sent.append((message, to_addrs))

    def quit(self):
        pass


@pytest.fixture
def fake(monkeypatch):
    FakeIMAP.instances, FakeSMTP.sent = [], []
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    asked = []
    monkeypatch.setattr(mail_tool, "ask_to_send", lambda summary: asked.append(summary) or None)
    return asked


def connected(fake):
    assert mail_tool.connect("me@naver.com", PASSWORD) == {"ok": True, "address": "me@naver.com", "provider": "naver"}


def call(**args):
    return json.loads(mail_tool.mail(args))


def test_korean_folder_names_round_trip():
    assert mail_tool.decode_folder("&vPSwuLpUx3zVaA-") == "보낸메일함"
    assert mail_tool.encode_folder("보낸메일함") == "&vPSwuLpUx3zVaA-"
    assert mail_tool.encode_folder("A&B 휴지통") == "A&-B &1zTJwNG1-"
    assert mail_tool.decode_folder("A&-B") == "A&B"


def test_connect_keeps_an_account_only_after_a_real_sign_in(fake):
    from hermes_cli.config import get_env_value

    assert mail_tool.connect("me@gmail.com", PASSWORD) == {"ok": False, "reason": "unknown"}
    assert mail_tool.connect("me@naver.com", "wrong")["reason"] == "login"
    assert mail_tool.accounts() == []
    connected(fake)
    assert FakeIMAP.instances[-1].host == "imap.naver.com"
    assert mail_tool.accounts() == [{"address": "me@naver.com", "provider": "naver"}]
    # Spaces in the shown app password are dropped; the password is in .env, not in config.
    assert get_env_value("CREMA_MAIL_PASSWORD_ME_NAVER_COM") == "abcdefghijkl"
    mail_tool.remove("me@naver.com")
    assert mail_tool.accounts() == [] and not get_env_value("CREMA_MAIL_PASSWORD_ME_NAVER_COM")


def test_the_terminal_never_gets_a_mail_password():
    from tools.environments.local_env_policy import _is_hermes_internal_secret

    assert _is_hermes_internal_secret("CREMA_MAIL_PASSWORD_ME_NAVER_COM")


def test_folders_search_and_read_in_korean(fake):
    connected(fake)
    folders = call(action="folders")["folders"]
    assert folders == [{"name": "INBOX", "role": "inbox"}, {"name": "보낸메일함", "role": "sent"}, {"name": "휴지통", "role": "trash"}]
    found = call(action="search", query="견적", unread_only=True)
    assert [m["uid"] for m in found["mails"]] == ["9", "7"]  # newest first
    assert found["mails"][1]["subject"] == "견적서 보내드립니다" and found["mails"][0]["unread"] is True
    args, literal = FakeIMAP.instances[-1].searches[-1]
    assert args == ("CHARSET", "UTF-8", "UNSEEN", "TEXT") and literal == "견적".encode()
    read = call(action="read", uid="7")
    assert read["body"] == "안녕하세요. 견적서를 첨부합니다." and read["from"].startswith("김철수")


def test_send_goes_only_after_the_user_allows_it_and_lands_in_the_sent_folder(fake, monkeypatch):
    connected(fake)
    monkeypatch.setattr(mail_tool, "ask_to_send", lambda summary: "not sent: the user declined.")
    assert call(action="send", to=["a@example.com"], subject="안녕", body="본문")["sent"] is False
    assert FakeSMTP.sent == []
    asked = []
    monkeypatch.setattr(mail_tool, "ask_to_send", lambda summary: asked.append(summary) or None)
    result = call(action="send", reply_to_uid="7", body="잘 받았습니다.")
    assert result["sent"] is True and result["kept_in"] == "보낸메일함"
    message, to = FakeSMTP.sent[-1]
    assert to == ["kim@example.com"] and message["Subject"] == "Re: 견적서 보내드립니다"
    assert message["In-Reply-To"] == "<m1@example.com>"
    assert asked == ["보내는 사람: me@naver.com · 받는 사람: kim@example.com · 제목: Re: 견적서 보내드립니다"]
    folder, flags, _ = FakeIMAP.instances[-1].appended[-1]
    assert folder == '"&vPSwuLpUx3zVaA-"' and flags == "(\\Seen)"


def test_delete_is_a_move_to_trash(fake):
    connected(fake)
    assert call(action="move", uid="9", to_folder="trash") == {"moved": "9", "from": "INBOX", "to": "휴지통"}
    assert FakeIMAP.instances[-1].stored == [("MOVE", ("9", '"&1zTJwNG1-"'))]
