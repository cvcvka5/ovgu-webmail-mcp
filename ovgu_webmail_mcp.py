#!/usr/bin/env python3
"""MCP server (read-only) for the OVGU mailbox behind https://webmail.ovgu.de.

webmail.ovgu.de is a Roundcube front end for the Cyrus IMAP server, so this
server talks IMAP directly (cyrus.ovgu.de:993) instead of scraping the web UI.
All message access uses BODY.PEEK and read-only SELECT: nothing is marked read,
moved, deleted or sent.

Env vars:
  OVGU_IMAP_USER      OVGU account username (NOT the e-mail address)
  OVGU_IMAP_PASSWORD  OVGU account password
  OVGU_IMAP_HOST      default: cyrus.ovgu.de
  OVGU_IMAP_PORT      default: 993
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import email
import email.header
import email.message
import email.policy
import email.utils
import imaplib
import json
import os
import re
import sys
from datetime import date
from enum import Enum
from html.parser import HTMLParser
from typing import Any, Optional
from pathlib import Path
import dotenv
dotenv.load_dotenv(Path(__file__).with_name(".env"))

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

mcp = FastMCP("ovgu_webmail_mcp")

TIMEOUT = 30
MAX_BODY_CHARS_DEFAULT = 8000
MAX_MESSAGE_BYTES = 25 * 1024 * 1024
HEADER_FIELDS = "FROM TO CC SUBJECT DATE MESSAGE-ID"


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


class MailError(Exception):
    """Actionable error shown to the model/user."""


# --------------------------------------------------------------------------
# Connection / helpers
# --------------------------------------------------------------------------
def _connect() -> imaplib.IMAP4_SSL:
    user = os.environ.get("OVGU_IMAP_USER")
    password = os.environ.get("OVGU_IMAP_PASSWORD")
    host = os.environ.get("OVGU_IMAP_HOST", "cyrus.ovgu.de")
    port = int(os.environ.get("OVGU_IMAP_PORT", "993"))
    if not user or not password:
        raise MailError(
            "OVGU_IMAP_USER and OVGU_IMAP_PASSWORD are not set. Use your OVGU "
            "account username (not the e-mail address) and its password."
        )
    try:
        conn = imaplib.IMAP4_SSL(host, port, timeout=TIMEOUT)
        conn.login(user, password)
    except imaplib.IMAP4.error as e:
        raise MailError(f"IMAP login failed ({_s(e)}). Check username (not e-mail address) and password.")
    except OSError as e:
        raise MailError(f"Cannot reach {host}:{port} ({e}). Check network/VPN and OVGU_IMAP_HOST.")
    return conn


def _s(x: Any) -> str:
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x)


def _ok(typ: str, data: Any, what: str) -> Any:
    if typ != "OK":
        raise MailError(f"IMAP {what} failed: {' '.join(_s(d) for d in (data or []))}")
    return data


# IMAP modified UTF-7 (RFC 3501 5.1.3) for non-ASCII folder names
def _utf7_decode(s: str) -> str:
    def repl(m: re.Match) -> str:
        if m.group(1) == "":
            return "&"
        b64 = m.group(1).replace(",", "/")
        b64 += "=" * (-len(b64) % 4)
        try:
            return base64.b64decode(b64).decode("utf-16-be")
        except (binascii.Error, UnicodeDecodeError):
            return m.group(0)

    return re.sub(r"&([^-]*)-", repl, s)


def _utf7_encode(s: str) -> str:
    out, buf = [], ""

    def flush() -> None:
        nonlocal buf
        if buf:
            b = base64.b64encode(buf.encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append(f"&{b}-")
            buf = ""

    for ch in s:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf += ch
    flush()
    return "".join(out)


def _quote(folder: str) -> str:
    enc = _utf7_encode(folder).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{enc}"'


def _select(conn: imaplib.IMAP4_SSL, folder: str) -> int:
    typ, data = conn.select(_quote(folder), readonly=True)
    if typ != "OK":
        raise MailError(f"Folder '{folder}' not found. Use webmail_list_folders to see valid names.")
    return int(_s(data[0]) or 0)


def _dec_header(v: Optional[str]) -> str:
    if not v:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(v))).strip()
    except Exception:
        return v.strip()


class _Strip(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    p = _Strip()
    p.feed(html)
    text = "".join(p.parts)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def _parse_fetch(data: list) -> list[dict]:
    """Parse imaplib FETCH response into [{uid, flags, size, headers(bytes)}]."""
    out = []
    for item in data:
        if not isinstance(item, tuple):
            continue
        prefix, payload = _s(item[0]), item[1]
        uid = re.search(r"UID (\d+)", prefix)
        flags = re.search(r"FLAGS \(([^)]*)\)", prefix)
        size = re.search(r"RFC822\.SIZE (\d+)", prefix)
        if not uid:
            continue
        out.append(
            {
                "uid": int(uid.group(1)),
                "flags": flags.group(1).split() if flags and flags.group(1) else [],
                "size": int(size.group(1)) if size else None,
                "raw": payload,
            }
        )
    return out


def _summary(entry: dict) -> dict:
    msg = email.message_from_bytes(entry["raw"], policy=email.policy.default)
    return {
        "uid": entry["uid"],
        "from": _dec_header(msg.get("From")),
        "to": _dec_header(msg.get("To")),
        "subject": _dec_header(msg.get("Subject")),
        "date": _dec_header(msg.get("Date")),
        "unread": "\\Seen" not in entry["flags"],
        "flagged": "\\Flagged" in entry["flags"],
        "answered": "\\Answered" in entry["flags"],
        "size_bytes": entry["size"],
    }


def _fetch_summaries(conn: imaplib.IMAP4_SSL, uids: list[int]) -> list[dict]:
    if not uids:
        return []
    uidset = ",".join(str(u) for u in uids)
    typ, data = conn.uid(
        "FETCH", uidset, f"(FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])"
    )
    entries = {e["uid"]: _summary(e) for e in _parse_fetch(_ok(typ, data, "FETCH"))}
    return [entries[u] for u in uids if u in entries]  # keep requested order


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


def _render_messages(msgs: list[dict], meta: dict, fmt: ResponseFormat) -> str:
    if fmt == ResponseFormat.JSON:
        return json.dumps({**meta, "messages": msgs}, ensure_ascii=False, indent=2)
    lines = [f"**{meta.get('folder', '')}** — {meta.get('showing', len(msgs))} of {meta.get('total', '?')} messages"]
    if meta.get("next_offset") is not None:
        lines.append(f"(more available: offset={meta['next_offset']})")
    for m in msgs:
        mark = "●" if m["unread"] else " "
        lines.append(f"- {mark} `{m['uid']}` {m['date']} | {m['from']} | {m['subject']}")
    return "\n".join(lines)


def _err(e: Exception) -> str:
    if isinstance(e, MailError):
        return f"Error: {e}"
    if isinstance(e, (imaplib.IMAP4.error, OSError)):
        return f"Error: IMAP/network problem: {e}. Retry; check network/VPN."
    log(f"unexpected: {e!r}")
    return f"Error: unexpected {type(e).__name__}: {e}"


# --------------------------------------------------------------------------
# Tool inputs
# --------------------------------------------------------------------------
class _Base(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="'markdown' (default) or 'json'")


class ListFoldersInput(_Base):
    pass


class ListMessagesInput(_Base):
    folder: str = Field(default="INBOX", description="Folder name, e.g. 'INBOX', 'Sent'")
    limit: int = Field(default=20, ge=1, le=100, description="Max messages to return")
    offset: int = Field(default=0, ge=0, description="Skip this many newest messages (pagination)")
    unread_only: bool = Field(default=False, description="Only unread messages")


class SearchInput(_Base):
    folder: str = Field(default="INBOX", description="Folder to search")
    text: Optional[str] = Field(default=None, description="Free text in headers or body, e.g. 'Forschungspanel'")
    from_: Optional[str] = Field(default=None, alias="from", description="Sender substring, e.g. 'github.com'")
    subject: Optional[str] = Field(default=None, description="Subject substring")
    since: Optional[date] = Field(default=None, description="Received on/after YYYY-MM-DD")
    before: Optional[date] = Field(default=None, description="Received before YYYY-MM-DD")
    unread_only: bool = Field(default=False)
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class GetMessageInput(_Base):
    folder: str = Field(default="INBOX")
    uid: int = Field(..., ge=1, description="Message UID from list/search results")
    max_chars: int = Field(default=MAX_BODY_CHARS_DEFAULT, ge=200, le=100000, description="Truncate body to this many characters")


# --------------------------------------------------------------------------
# Tools (all read-only)
# --------------------------------------------------------------------------
RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}


def _list_folders_sync() -> list[dict]:
    conn = _connect()
    try:
        typ, data = conn.list()
        folders = []
        for raw in _ok(typ, data, "LIST"):
            m = re.match(r'\((?P<flags>.*?)\) (?P<sep>"[^"]*"|NIL) (?P<name>.+)$', _s(raw))
            if not m or "\\Noselect" in m.group("flags"):
                continue
            name = _utf7_decode(m.group("name").strip().strip('"'))
            info = {"name": name, "messages": None, "unread": None}
            typ2, st = conn.status(_quote(name), "(MESSAGES UNSEEN)")
            if typ2 == "OK" and st and st[0]:
                s = _s(st[0])
                mm, uu = re.search(r"MESSAGES (\d+)", s), re.search(r"UNSEEN (\d+)", s)
                info["messages"] = int(mm.group(1)) if mm else None
                info["unread"] = int(uu.group(1)) if uu else None
            folders.append(info)
        return folders
    finally:
        _logout(conn)


def _logout(conn: imaplib.IMAP4_SSL) -> None:
    try:
        conn.logout()
    except Exception:
        pass


@mcp.tool(name="webmail_list_folders", annotations={"title": "List mail folders", **RO})
async def webmail_list_folders(params: ListFoldersInput) -> str:
    """List mailbox folders with total and unread counts (OVGU webmail / Cyrus IMAP)."""
    try:
        folders = await asyncio.to_thread(_list_folders_sync)
    except Exception as e:
        return _err(e)
    if params.response_format == ResponseFormat.JSON:
        return json.dumps({"folders": folders}, ensure_ascii=False, indent=2)
    return "\n".join(f"- {f['name']}: {f['messages']} messages, {f['unread']} unread" for f in folders)


def _list_sync(p: ListMessagesInput) -> str:
    conn = _connect()
    try:
        _select(conn, p.folder)
        typ, data = conn.uid("SEARCH", None, "UNSEEN" if p.unread_only else "ALL")
        uids = [int(x) for x in _s(_ok(typ, data, "SEARCH")[0]).split()]
        uids.sort(reverse=True)  # newest first
        page = uids[p.offset : p.offset + p.limit]
        msgs = _fetch_summaries(conn, page)
        nxt = p.offset + p.limit if p.offset + p.limit < len(uids) else None
        meta = {"folder": p.folder, "total": len(uids), "showing": len(msgs), "offset": p.offset, "next_offset": nxt}
        return _render_messages(msgs, meta, p.response_format)
    finally:
        _logout(conn)


@mcp.tool(name="webmail_list_messages", annotations={"title": "List messages", **RO})
async def webmail_list_messages(params: ListMessagesInput) -> str:
    """List message headers (newest first) in a folder: uid, sender, subject, date, unread flag.

    Use `offset` to paginate. Does not mark messages as read.
    """
    try:
        return await asyncio.to_thread(_list_sync, params)
    except Exception as e:
        return _err(e)


def _search_sync(p: SearchInput) -> str:
    # Each criterion is (keyword, value|None). Non-ASCII values must be sent as an
    # IMAP literal, which imaplib supports for the LAST argument only.
    crit: list[tuple[str, Optional[str]]] = []
    if p.unread_only:
        crit.append(("UNSEEN", None))
    if p.since:
        crit.append(("SINCE", p.since.strftime("%d-%b-%Y")))
    if p.before:
        crit.append(("BEFORE", p.before.strftime("%d-%b-%Y")))
    for key, val in (("FROM", p.from_), ("SUBJECT", p.subject), ("TEXT", p.text)):
        if val:
            crit.append((key, val))
    if not crit:
        crit.append(("ALL", None))

    non_ascii = [c for c in crit if c[1] and not c[1].isascii()]
    if len(non_ascii) > 1:
        raise MailError("Only one non-ASCII search term (e.g. umlauts) is supported per search; split the search.")

    args: list[str] = []
    literal: Optional[bytes] = None
    for key, val in crit:
        if val is None:
            args.append(key)
        elif val.isascii():
            args += [key, '"' + val.replace("\\", "\\\\").replace('"', '\\"') + '"']
    if non_ascii:
        key, val = non_ascii[0]
        args += [key, ""]  # placeholder replaced by literal
        literal = val.encode("utf-8")

    conn = _connect()
    try:
        _select(conn, p.folder)
        if literal is not None:
            args = args[:-1]  # literal is appended as final argument by imaplib
            conn.literal = literal
            typ, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *args)
        else:
            typ, data = conn.uid("SEARCH", None, *args)
        uids = sorted((int(x) for x in _s(_ok(typ, data, "SEARCH")[0]).split()), reverse=True)
        page = uids[p.offset : p.offset + p.limit]
        msgs = _fetch_summaries(conn, page)
        nxt = p.offset + p.limit if p.offset + p.limit < len(uids) else None
        meta = {"folder": p.folder, "total": len(uids), "showing": len(msgs), "offset": p.offset, "next_offset": nxt}
        return _render_messages(msgs, meta, p.response_format)
    finally:
        _logout(conn)


@mcp.tool(name="webmail_search_messages", annotations={"title": "Search messages", **RO})
async def webmail_search_messages(params: SearchInput) -> str:
    """Search a folder by free text, sender, subject, date range and/or unread status (criteria are ANDed).

    Returns the same fields as webmail_list_messages, newest first, paginated.
    """
    try:
        return await asyncio.to_thread(_search_sync, params)
    except Exception as e:
        return _err(e)


def _get_sync(p: GetMessageInput) -> str:
    conn = _connect()
    try:
        _select(conn, p.folder)
        typ, data = conn.uid("FETCH", str(p.uid), "(RFC822.SIZE)")
        info = _parse_size(_ok(typ, data, "FETCH"))
        if info is None:
            raise MailError(f"No message with UID {p.uid} in '{p.folder}'.")
        if info > MAX_MESSAGE_BYTES:
            raise MailError(f"Message is {info // 1024 // 1024} MB (> {MAX_MESSAGE_BYTES // 1024 // 1024} MB limit); open it in webmail.")
        typ, data = conn.uid("FETCH", str(p.uid), "(FLAGS BODY.PEEK[])")
        entries = _parse_fetch(_ok(typ, data, "FETCH"))
        if not entries:
            raise MailError(f"No message with UID {p.uid} in '{p.folder}'.")
        e = entries[0]
        msg = email.message_from_bytes(e["raw"], policy=email.policy.default)
        return _render_message(msg, e, p)
    finally:
        _logout(conn)


def _parse_size(data: list) -> Optional[int]:
    for d in data:
        m = re.search(r"RFC822\.SIZE (\d+)", _s(d[0] if isinstance(d, tuple) else d))
        if m:
            return int(m.group(1))
    return None


def _render_message(msg: email.message.EmailMessage, entry: dict, p: GetMessageInput) -> str:
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        body = body_part.get_content()
        if body_part.get_content_type() == "text/html":
            body = _html_to_text(body)
    truncated = len(body) > p.max_chars
    body = body[: p.max_chars]
    attachments = [
        {
            "filename": _dec_header(a.get_filename()) or "(unnamed)",
            "content_type": a.get_content_type(),
            "size_bytes": len(a.get_payload(decode=True) or b""),
        }
        for a in msg.iter_attachments()
    ]
    result = {
        "uid": p.uid,
        "folder": p.folder,
        "from": _dec_header(msg.get("From")),
        "to": _dec_header(msg.get("To")),
        "cc": _dec_header(msg.get("Cc")),
        "subject": _dec_header(msg.get("Subject")),
        "date": _dec_header(msg.get("Date")),
        "unread": "\\Seen" not in entry["flags"],
        "attachments": attachments,
        "body": body,
        "body_truncated": truncated,
    }
    if p.response_format == ResponseFormat.JSON:
        return json.dumps(result, ensure_ascii=False, indent=2)
    att = "\n".join(f"  - {a['filename']} ({a['content_type']}, {a['size_bytes']} B)" for a in attachments) or "  none"
    return (
        f"**{result['subject']}**\nFrom: {result['from']}\nTo: {result['to']}\n"
        + (f"Cc: {result['cc']}\n" if result["cc"] else "")
        + f"Date: {result['date']}\nAttachments:\n{att}\n\n{body}"
        + ("\n\n[truncated: raise max_chars to see more]" if truncated else "")
    )


@mcp.tool(name="webmail_get_message", annotations={"title": "Read a message", **RO})
async def webmail_get_message(params: GetMessageInput) -> str:
    """Read one message by UID: headers, plain-text body (HTML converted) and attachment list.

    Read-only: the message is not marked as read. Attachment contents are not returned.
    """
    try:
        return await asyncio.to_thread(_get_sync, params)
    except Exception as e:
        return _err(e)


if __name__ == "__main__":
    mcp.run()  # stdio
