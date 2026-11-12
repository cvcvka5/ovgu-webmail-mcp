"""Offline tests: a fake IMAP connection replaces the real server."""
import asyncio
import json
from email.message import EmailMessage

import pytest

import ovgu_webmail_mcp as srv

MSG = EmailMessage()
MSG["From"] = "=?utf-8?q?M=C3=BCller?= <m@example.de>"
MSG["To"] = "ulus@st.ovgu.de"
MSG["Subject"] = "Forschungspanels an der OVGU"
MSG["Date"] = "Wed, 11 Nov 2026 14:47:00 +0100"
MSG.set_content("Hallo,\nbitte teilnehmen.\n")
MSG.add_attachment(b"abc", maintype="application", subtype="pdf", filename="info.pdf")
RAW = MSG.as_bytes()
HDR = RAW.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"


class FakeConn:
    literal = None

    def __init__(self):
        self.calls = []

    def logout(self):
        pass

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Sent"', b'(\\Noselect) "/" "X"']

    def status(self, name, what):
        return "OK", [name.encode() + b" (MESSAGES 3 UNSEEN 1)"]

    def select(self, name, readonly=False):
        assert readonly
        return ("OK", [b"3"]) if "INBOX" in name else ("NO", [b"nope"])

    def uid(self, cmd, *args):
        self.calls.append((cmd, args, self.literal))
        if cmd == "SEARCH":
            return "OK", [b"1 2 3"]
        what = args[1]
        if "HEADER.FIELDS" in what:
            return "OK", [
                (b"1 (UID 3 FLAGS () RFC822.SIZE 500 BODY[HEADER.FIELDS (FROM)] {10}", HDR),
                b")",
                (b"2 (UID 2 FLAGS (\\Seen) RFC822.SIZE 400 BODY[HEADER.FIELDS (FROM)] {10}", HDR),
                b")",
            ]
        if what == "(RFC822.SIZE)":
            return "OK", [b"1 (UID 3 RFC822.SIZE 500)"]
        return "OK", [(b"1 (UID 3 FLAGS () BODY[] {99}", RAW), b")"]


@pytest.fixture
def fake(monkeypatch):
    c = FakeConn()
    monkeypatch.setattr(srv, "_connect", lambda: c)
    return c


def run(coro):
    return asyncio.run(coro)


def test_utf7_roundtrip():
    for name in ["INBOX", "Entwürfe", "A&B", "日本"]:
        assert srv._utf7_decode(srv._utf7_encode(name)) == name


def test_list_folders(fake):
    out = run(srv.webmail_list_folders(srv.ListFoldersInput(response_format="json")))
    names = [f["name"] for f in json.loads(out)["folders"]]
    assert names == ["INBOX", "Sent"]  # \Noselect skipped


def test_list_messages_newest_first_and_unread(fake):
    out = json.loads(run(srv.webmail_list_messages(srv.ListMessagesInput(response_format="json"))))
    assert out["total"] == 3
    assert out["messages"][0]["uid"] == 3 and out["messages"][0]["unread"] is True
    assert out["messages"][1]["unread"] is False
    assert out["messages"][0]["from"].startswith("Müller")


def test_list_markdown(fake):
    out = run(srv.webmail_list_messages(srv.ListMessagesInput()))
    assert "Forschungspanels" in out and "●" in out


def test_unknown_folder(fake):
    out = run(srv.webmail_list_messages(srv.ListMessagesInput(folder="Nope")))
    assert out.startswith("Error:") and "webmail_list_folders" in out


def test_search_ascii(fake):
    run(srv.webmail_search_messages(srv.SearchInput(**{"from": "github", "unread_only": True})))
    cmd, args, lit = [c for c in fake.calls if c[0] == "SEARCH"][0]
    assert args == (None, "UNSEEN", "FROM", '"github"') and lit is None


def test_search_non_ascii_uses_literal(fake):
    run(srv.webmail_search_messages(srv.SearchInput(text="Prüfung")))
    cmd, args, lit = [c for c in fake.calls if c[0] == "SEARCH"][0]
    assert args == ("CHARSET", "UTF-8", "TEXT") and lit == "Prüfung".encode()


def test_search_two_non_ascii_rejected(fake):
    out = run(srv.webmail_search_messages(srv.SearchInput(text="Prüfung", subject="Übung")))
    assert out.startswith("Error:")


def test_get_message(fake):
    out = json.loads(run(srv.webmail_get_message(srv.GetMessageInput(uid=3, response_format="json"))))
    assert "bitte teilnehmen" in out["body"]
    assert out["attachments"][0]["filename"] == "info.pdf"
    assert out["unread"] is True


def test_get_message_truncates(fake):
    out = json.loads(run(srv.webmail_get_message(srv.GetMessageInput(uid=3, max_chars=200, response_format="json"))))
    assert out["body_truncated"] is False


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("OVGU_IMAP_USER", raising=False)
    out = run(srv.webmail_list_folders(srv.ListFoldersInput()))
    assert "OVGU_IMAP_USER" in out


def test_tools_registered():
    names = {t.name for t in run(srv.mcp.list_tools())}
    assert names == {"webmail_list_folders", "webmail_list_messages", "webmail_search_messages", "webmail_get_message"}
