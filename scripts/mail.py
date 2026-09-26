#!/usr/bin/env python3
"""Safe IMAP search/move/mark helper. No delete command exists on purpose."""
import argparse
import imaplib
import json
import os
import re
import sys
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header, make_header


def decode(value):
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def connect(args):
    password = os.environ.get("ICLOUD_MAIL_PASSWORD")
    if not password:
        sys.exit("ICLOUD_MAIL_PASSWORD is not set in the environment; run this via the gateway-host exec path so the secret store can inject it.")
    # Defensive: stored secrets can pick up a trailing newline/whitespace from
    # how they were entered (e.g. `--value-file -` capturing the Enter key).
    # An embedded newline breaks IMAP's quoted-string LOGIN syntax server-side.
    password = password.strip()
    conn = imaplib.IMAP4_SSL(args.host, args.port)
    conn.login(args.user, password)
    conn.select(args.mailbox, readonly=False)
    return conn


def cmd_rename_folder(args):
    conn = connect(args)
    typ, data = conn.rename(args.name, args.to)
    if typ != "OK":
        sys.exit(f"RENAME failed: {data}")
    print(json.dumps({"renamed_from": args.name, "renamed_to": args.to}))
    conn.logout()


def cmd_subscribe_folder(args):
    conn = connect(args)
    typ, data = conn.subscribe(args.name)
    if typ != "OK":
        sys.exit(f"SUBSCRIBE failed: {data}")
    print(json.dumps({"subscribed": args.name}))
    conn.logout()


def cmd_read(args):
    conn = connect(args)
    conn.select(args.mailbox, readonly=True)
    typ, msgdata = conn.uid("fetch", args.uid, "(BODY.PEEK[])")
    if typ != "OK" or not msgdata or msgdata[0] is None:
        sys.exit(f"FETCH failed: {msgdata}")
    raw = msgdata[0][1]
    msg = message_from_bytes(raw)

    def extract(mime_type):
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == mime_type and "attachment" not in str(part.get("Content-Disposition", "")):
                    payload = part.get_payload(decode=True)
                    if payload is not None:
                        charset = part.get_content_charset() or "utf-8"
                        return payload.decode(charset, errors="replace")
            return None
        if msg.get_content_type() == mime_type:
            payload = msg.get_payload(decode=True)
            if payload is not None:
                charset = msg.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        return None

    body = extract("text/plain")
    body_type = "text/plain"
    if body is None:
        body = extract("text/html")
        body_type = "text/html"
        if body is not None:
            body = re.sub(r"<[^>]+>", " ", body)
            body = re.sub(r"\s+", " ", body).strip()

    max_chars = args.max_chars
    truncated = False
    if body is not None and len(body) > max_chars:
        body = body[:max_chars]
        truncated = True

    print(json.dumps({
        "uid": args.uid,
        "from": decode(msg.get("From")),
        "subject": decode(msg.get("Subject")),
        "date": msg.get("Date"),
        "body_type": body_type,
        "body": body,
        "truncated": truncated,
    }))
    conn.logout()


def cmd_create_folder(args):
    conn = connect(args)
    typ, data = conn.create(args.name)
    if typ != "OK":
        sys.exit(f"CREATE failed: {data}")
    # Subscribe so mail clients (Apple Mail, iOS Mail) actually show/sync the
    # folder instead of silently ignoring an unsubscribed mailbox.
    sub_typ, sub_data = conn.subscribe(args.name)
    print(json.dumps({"created": args.name, "subscribed": sub_typ == "OK"}))
    conn.logout()


def cmd_list_folders(args):
    conn = connect(args)
    typ, data = conn.list()
    if typ != "OK":
        sys.exit(f"LIST failed: {data}")
    for line in data:
        text = line.decode(errors="replace") if isinstance(line, bytes) else str(line)
        m = re.search(r'"([^"]+)"$', text)
        print(m.group(1) if m else text)
    conn.logout()


def build_search_criteria(args):
    crit = []
    if args.unseen:
        crit.append("UNSEEN")
    if args.seen:
        crit.append("SEEN")
    if args.from_:
        crit += ["FROM", f'"{args.from_}"']
    if args.subject:
        crit += ["SUBJECT", f'"{args.subject}"']
    if args.since:
        d = datetime.strptime(args.since, "%Y-%m-%d").strftime("%d-%b-%Y")
        crit += ["SINCE", d]
    if args.before:
        d = datetime.strptime(args.before, "%Y-%m-%d").strftime("%d-%b-%Y")
        crit += ["BEFORE", d]
    return crit or ["ALL"]


def cmd_search(args):
    conn = connect(args)
    conn.select(args.mailbox, readonly=True)
    typ, data = conn.uid("search", None, *build_search_criteria(args))
    if typ != "OK":
        sys.exit(f"SEARCH failed: {data}")
    uids = data[0].split() if data and data[0] else []
    if args.limit:
        uids = uids[-args.limit:]
    if not uids:
        conn.logout()
        return
    # Batched UID FETCH per chunk instead of one round-trip per message (too
    # slow) or one giant request for everything (times out / overwhelms the
    # response parser on large result sets).
    chunk_size = 50
    for start in range(0, len(uids), chunk_size):
        chunk = uids[start:start + chunk_size]
        uid_set = b",".join(chunk)
        typ, msgdata = conn.uid("fetch", uid_set, "(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        if typ != "OK":
            sys.exit(f"FETCH failed: {msgdata}")
        for item in msgdata:
            if not isinstance(item, tuple):
                continue
            meta, header_bytes = item[0], item[1]
            meta_text = meta.decode(errors="replace") if isinstance(meta, bytes) else str(meta)
            uid_match = re.search(r'UID (\d+)', meta_text)
            uid_str = uid_match.group(1) if uid_match else "?"
            msg = message_from_bytes(header_bytes)
            print(json.dumps({
                "uid": uid_str,
                "from": decode(msg.get("From")),
                "subject": decode(msg.get("Subject")),
                "date": msg.get("Date"),
                "flags": re.findall(r'\\\w+', meta_text),
            }))
    conn.logout()


def cmd_mark(args):
    conn = connect(args)
    flag_op = "+FLAGS" if args.seen else "-FLAGS"
    typ, data = conn.uid("store", args.uid, flag_op, "(\\Seen)")
    if typ != "OK":
        sys.exit(f"STORE failed: {data}")
    print(json.dumps({"uid": args.uid, "seen": bool(args.seen)}))
    conn.logout()


def cmd_move(args):
    conn = connect(args)
    typ, data = conn.uid("move", args.uid, args.to)
    if typ != "OK":
        typ, data = conn.uid("copy", args.uid, args.to)
        if typ != "OK":
            sys.exit(f"COPY fallback failed: {data}")
        conn.uid("store", args.uid, "+FLAGS", "(\\Deleted)")
        conn.expunge()
    print(json.dumps({"uid": args.uid, "moved_to": args.to}))
    conn.logout()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=os.environ.get("ICLOUD_MAIL_HOST", "imap.mail.me.com"))
    p.add_argument("--port", type=int, default=993)
    p.add_argument("--user", default=os.environ.get("ICLOUD_MAIL_USER", ""))
    p.add_argument("--mailbox", default="INBOX")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list-folders")

    s = sub.add_parser("search")
    s.add_argument("--unseen", action="store_true")
    s.add_argument("--seen", action="store_true")
    s.add_argument("--from", dest="from_")
    s.add_argument("--subject")
    s.add_argument("--since")
    s.add_argument("--before")
    s.add_argument("--limit", type=int, default=50)

    m = sub.add_parser("mark")
    m.add_argument("--uid", required=True)
    g = m.add_mutually_exclusive_group(required=True)
    g.add_argument("--seen", dest="seen", action="store_true")
    g.add_argument("--unseen", dest="seen", action="store_false")

    mv = sub.add_parser("move")
    mv.add_argument("--uid", required=True)
    mv.add_argument("--to", required=True)

    cf = sub.add_parser("create-folder")
    cf.add_argument("--name", required=True)

    rf = sub.add_parser("rename-folder")
    rf.add_argument("--name", required=True, help="Current folder name")
    rf.add_argument("--to", required=True, help="New folder name")

    sf = sub.add_parser("subscribe-folder")
    sf.add_argument("--name", required=True)

    r = sub.add_parser("read")
    r.add_argument("--uid", required=True)
    r.add_argument("--max-chars", type=int, default=4000, dest="max_chars")

    args = p.parse_args()
    if not args.user:
        sys.exit("--user is required (or set ICLOUD_MAIL_USER)")

    {
        "list-folders": cmd_list_folders,
        "search": cmd_search,
        "mark": cmd_mark,
        "move": cmd_move,
        "create-folder": cmd_create_folder,
        "rename-folder": cmd_rename_folder,
        "subscribe-folder": cmd_subscribe_folder,
        "read": cmd_read,
    }[args.command](args)


if __name__ == "__main__":
    main()