# ovgu-webmail-mcp

A small, read-only MCP server that lets Claude (or any MCP client) look through your OVGU mailbox.

webmail.ovgu.de is just Roundcube sitting on top of the university's Cyrus IMAP server, so this skips the web page entirely and talks IMAP to `cyrus.ovgu.de:993`. It can list folders, list and search messages, and read a message. It cannot send, move, delete or mark anything as read. Every fetch uses `BODY.PEEK` and every folder is opened read-only.

I wrote it to ask things like "what's unread from the Studierendenrat this week?" without opening the browser. It hasn't had much real-world use yet, so expect rough edges.

## Setup

You need Python 3.10+ and an OVGU account.

```bash
git clone https://github.com/cvcvka5/ovgu-webmail-mcp.git
cd ovgu-webmail-mcp
pip install -r requirements.txt
```

The SDK is pinned below version 2 because the code uses `FastMCP`, which mcp 2.x renamed.

## Configuration

The server reads its settings from environment variables.

| Variable | Meaning | Default |
|---|---|---|
| `OVGU_IMAP_USER` | Your OVGU account username | none |
| `OVGU_IMAP_PASSWORD` | Your OVGU account password | none |
| `OVGU_IMAP_HOST` | IMAP server | `cyrus.ovgu.de` |
| `OVGU_IMAP_PORT` | IMAP port (TLS) | `993` |

One thing that trips people up: the username is your account login, not your e-mail address. OVGU's own setup guides say the same.

Copy `.env` if you want a template. Don't commit real credentials.

## Using it with Claude

Claude Code:

```bash
claude mcp add ovgu-webmail \
  -e OVGU_IMAP_USER=your_username \
  -e OVGU_IMAP_PASSWORD=your_password \
  -- python /absolute/path/to/ovgu_webmail_mcp.py
```

Claude Desktop, in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ovgu-webmail": {
      "command": "python",
      "args": ["/absolute/path/to/ovgu_webmail_mcp.py"],
      "env": {
        "OVGU_IMAP_USER": "your_username",
        "OVGU_IMAP_PASSWORD": "your_password"
      }
    }
  }
}
```

To poke at it without a client, run `npx @modelcontextprotocol/inspector python ovgu_webmail_mcp.py`.

## Tools

All four tools are read-only and accept `response_format` set to `markdown` (the default) or `json`.

`webmail_list_folders` returns every folder with its message count and unread count.

`webmail_list_messages` returns headers for a folder, newest first. Options are `folder` (default `INBOX`), `limit` (up to 100), `offset` for paging, and `unread_only`.

`webmail_search_messages` filters a folder by `text`, `from`, `subject`, `since`, `before` and `unread_only`. Filters are combined with AND.

`webmail_get_message` takes a folder and a UID from the list or search results and returns the headers, the body as plain text (HTML is stripped) and a list of attachments. Bodies are cut off at `max_chars`, 8000 by default.

## Limits

- Search supports one non-ASCII term at a time, so "Prüfung" works but "Prüfung" plus "Übung" in the same search does not. Split it into two searches.
- Attachment contents are never returned, only names, types and sizes.
- Messages over 25 MB are refused. Open those in webmail.
- The password lives in plain text in your client config. Use a dedicated password if OVGU offers one, and keep the config file private.
- The offline tests use a fake IMAP connection. I haven't verified the server against the live OVGU mailbox from a clean checkout, so if something behaves oddly with real mail, open an issue.

## Tests

```bash
pip install pytest
pytest
```

## License

MIT. Add a `LICENSE` file before publishing.