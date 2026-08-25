#!/usr/bin/env python3
"""
mail_to_md.py - Email → Markdown pipeline for Obsidian / LLM Wiki

Polls an IMAP mailbox for new messages, converts each email to a Markdown
file in a target directory, extracts URLs from the body, fetches those pages,
and appends their content to the same .md file.

Usage:
    python3 mail_to_md.py --setup          # interactive first-time config
    python3 mail_to_md.py                  # single poll run
    python3 mail_to_md.py --daemon         # loop every N minutes (default 60)
    python3 mail_to_md.py --interval 30    # override poll interval

Config file : ~/.config/mail_to_md/config.ini
State file  : ~/.config/mail_to_md/state.json  (tracks processed UIDs)

Dependencies (pip install):
    requests  beautifulsoup4  html2text
"""

import argparse
import configparser
import email
import email.policy
import email.utils
import imaplib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
import html2text as html2text_lib

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CONFIG_DIR  = Path("~/.config/mail_to_md").expanduser()
CONFIG_FILE = CONFIG_DIR / "config.ini"
STATE_FILE  = CONFIG_DIR / "state.json"

# ---------------------------------------------------------------------------
# Patterns / constants
# ---------------------------------------------------------------------------

URL_RE = re.compile(r'https?://[^\s<>"\')\]\\,]+', re.IGNORECASE)

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# Extensions that are never worth fetching as text
SKIP_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".mp4", ".mp3", ".wav", ".ogg", ".webm",
    ".pdf", ".zip", ".gz", ".tar", ".rar", ".7z",
    ".exe", ".dmg", ".pkg", ".deb", ".rpm",
    ".woff", ".woff2", ".ttf", ".eot",
}

# Tags whose content is always boilerplate
STRIP_TAGS = {
    "script", "style", "nav", "header", "footer",
    "aside", "noscript", "iframe", "form", "button",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mail_to_md")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "imap": {
        "host":         "",
        "port":         "993",
        "username":     "",
        "password":     "",
        "folder":       "INBOX",
        "ssl":          "true",
        "mark_as_read": "false",
    },
    "output": {
        "directory":         "~/obsidian/Inbox",
        "filename_template": "{date}_{slug}.md",
    },
    "fetch": {
        "follow_urls":        "true",
        "timeout_seconds":    "15",
        "max_urls_per_email": "10",
        "max_content_chars":  "30000",
        "skip_domains":       "unsubscribe.example.com",  # comma-separated
    },
    "daemon": {
        "interval_minutes": "60",
    },
}


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    for section, values in DEFAULT_CONFIG.items():
        cfg[section] = values
    if CONFIG_FILE.exists():
        cfg.read(CONFIG_FILE)
    return cfg


def save_config(cfg: configparser.ConfigParser):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as fh:
        cfg.write(fh)
    CONFIG_FILE.chmod(0o600)  # protect stored password


def interactive_setup():
    cfg = load_config()
    print("\n=== mail_to_md first-time setup ===\n")

    def ask(prompt, current):
        val = input(f"{prompt} [{current}]: ").strip()
        return val or current

    cfg["imap"]["host"]     = ask("IMAP host (e.g. imap.gmail.com)", cfg["imap"]["host"])
    cfg["imap"]["port"]     = ask("IMAP port", cfg["imap"]["port"])
    cfg["imap"]["username"] = ask("Username / email address", cfg["imap"]["username"])
    cfg["imap"]["password"] = input("Password (stored in plaintext, chmod 600): ").strip() or cfg["imap"]["password"]
    cfg["imap"]["folder"]   = ask("Folder to watch", cfg["imap"]["folder"])

    mark = ask("Mark fetched messages as read? (true/false)", cfg["imap"]["mark_as_read"])
    cfg["imap"]["mark_as_read"] = mark

    cfg["output"]["directory"] = ask("Output directory for .md files", cfg["output"]["directory"])

    save_config(cfg)
    print(f"\nConfig written to {CONFIG_FILE}")
    print("Run without --setup to begin polling.\n")


# ---------------------------------------------------------------------------
# State — tracks IMAP UIDs we have already processed
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"processed_uids": [], "uidvalidity": {}}


def save_state(state: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def uid_key(folder: str, uid: str) -> str:
    return f"{folder}:{uid}"


def already_processed(state: dict, folder: str, uid: str) -> bool:
    return uid_key(folder, uid) in state["processed_uids"]


def mark_processed(state: dict, folder: str, uid: str):
    key = uid_key(folder, uid)
    if key not in state["processed_uids"]:
        state["processed_uids"].append(key)
    # cap to avoid unbounded growth
    state["processed_uids"] = state["processed_uids"][-20000:]
    save_state(state)


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

def slugify(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text[:max_len] or "untitled"


def unique_path(directory: Path, filename: str) -> Path:
    """Append -1, -2, … if the file already exists."""
    p = directory / filename
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    i = 1
    while True:
        candidate = directory / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


# ---------------------------------------------------------------------------
# Email parsing
# ---------------------------------------------------------------------------

def decode_bytes(payload: bytes, charset: Optional[str]) -> str:
    charset = charset or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def get_bodies(msg: email.message.Message) -> tuple[str, str]:
    """Return (plain_text, html_text) from a possibly multipart message."""
    plain = html = ""

    if msg.is_multipart():
        for part in msg.walk():
            ct  = part.get_content_type()
            cde = str(part.get("Content-Disposition", ""))
            if "attachment" in cde:
                continue
            raw = part.get_payload(decode=True)
            if not raw:
                continue
            charset = part.get_content_charset()
            if ct == "text/plain" and not plain:
                plain = decode_bytes(raw, charset)
            elif ct == "text/html" and not html:
                html = decode_bytes(raw, charset)
    else:
        raw = msg.get_payload(decode=True)
        if raw:
            charset = msg.get_content_charset()
            ct = msg.get_content_type()
            if ct == "text/html":
                html = decode_bytes(raw, charset)
            else:
                plain = decode_bytes(raw, charset)

    return plain, html


def html_to_md(html: str) -> str:
    h = html2text_lib.HTML2Text()
    h.ignore_links   = False
    h.ignore_images  = True
    h.body_width     = 0
    h.unicode_snob   = True
    h.protect_links  = False
    return h.handle(html)


def extract_urls(text: str) -> list[str]:
    """Return deduplicated list of http(s) URLs, preserving first-seen order."""
    seen: dict[str, None] = {}
    for url in URL_RE.findall(text):
        url = url.rstrip(".,;:!?")  # strip trailing punctuation
        seen.setdefault(url, None)
    return list(seen)


# ---------------------------------------------------------------------------
# Web content extraction
# ---------------------------------------------------------------------------

def should_skip(url: str, skip_domains: list[str]) -> bool:
    parsed = urlparse(url)
    ext = Path(parsed.path).suffix.lower()
    if ext in SKIP_EXTENSIONS:
        return True
    netloc = parsed.netloc.lower()
    return any(d and d in netloc for d in skip_domains)


def fetch_page(url: str, timeout: int, max_chars: int) -> Optional[str]:
    """Fetch a URL and return cleaned Markdown, or None / an error note."""
    try:
        resp = requests.get(url, headers=FETCH_HEADERS, timeout=timeout,
                            allow_redirects=True)
        resp.raise_for_status()

        ct = resp.headers.get("content-type", "")
        if "text/html" not in ct and "text/plain" not in ct:
            return f"*[Skipped — content type: `{ct}`]*"

        if "text/plain" in ct:
            return resp.text[:max_chars]

        soup = BeautifulSoup(resp.text, "html.parser")

        # Pull page title before stripping tags
        title_tag = soup.find("title")
        page_title = title_tag.get_text(strip=True) if title_tag else ""

        for tag in soup.find_all(STRIP_TAGS):
            tag.decompose()

        # Try progressively broader selectors for main content
        content = (
            soup.find("article")
            or soup.find("main")
            or soup.find(id=re.compile(r"content|article|main|post|body", re.I))
            or soup.find(class_=re.compile(r"content|article|main|post|entry|body", re.I))
            or soup.find("body")
            or soup
        )

        md = html_to_md(str(content))
        md = re.sub(r"\n{3,}", "\n\n", md).strip()

        header = ""
        if page_title:
            header = f"**Page:** {page_title}  \n"
        header += f"**URL:** {url}\n\n"

        return header + md[:max_chars]

    except requests.exceptions.Timeout:
        log.warning("  Timeout: %s", url)
        return f"*[Fetch timed out after {timeout}s]*"
    except requests.exceptions.TooManyRedirects:
        log.warning("  Too many redirects: %s", url)
        return f"*[Too many redirects]*"
    except requests.exceptions.RequestException as exc:
        log.warning("  Fetch error (%s): %s", url, exc)
        return f"*[Fetch failed: {exc}]*"
    except Exception as exc:
        log.warning("  Unexpected error fetching %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Markdown assembly
# ---------------------------------------------------------------------------

def build_md(
    subject: str,
    sender: str,
    date: datetime,
    body_md: str,
    page_contents: dict[str, str],
) -> str:
    date_str = date.strftime("%Y-%m-%d %H:%M")

    lines = [
        "---",
        "source: email",
        f"from: \"{sender}\"",
        f"date: {date_str}",
        f"subject: \"{subject}\"",
        "tags: [inbox, email]",
        "---",
        "",
        f"# {subject}",
        "",
        f"**From:** {sender}  ",
        f"**Date:** {date_str}",
        "",
        "## Email Body",
        "",
        body_md.strip(),
        "",
    ]

    if page_contents:
        lines += ["---", "", "## Extracted Web Content", ""]
        for url, content in page_contents.items():
            lines += [
                f"### {url}",
                "",
                content or "*[No content extracted]*",
                "",
            ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core poll loop
# ---------------------------------------------------------------------------

def poll_once(cfg: configparser.ConfigParser, state: dict):
    host     = cfg["imap"]["host"]
    port     = int(cfg["imap"]["port"])
    use_ssl  = cfg["imap"].getboolean("ssl")
    username = cfg["imap"]["username"]
    password = cfg["imap"]["password"]
    folder   = cfg["imap"]["folder"]
    mark_read = cfg["imap"].getboolean("mark_as_read")

    out_dir  = Path(cfg["output"]["directory"]).expanduser()
    template = cfg["output"]["filename_template"]

    follow_urls = cfg["fetch"].getboolean("follow_urls")
    timeout     = int(cfg["fetch"]["timeout_seconds"])
    max_urls    = int(cfg["fetch"]["max_urls_per_email"])
    max_chars   = int(cfg["fetch"]["max_content_chars"])
    skip_domains = [d.strip() for d in cfg["fetch"]["skip_domains"].split(",")]

    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("Connecting to %s:%d ...", host, port)

    try:
        conn = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
        conn.login(username, password)

        # Check UIDVALIDITY — if it changed, all old UIDs are invalid
        status, data = conn.select(folder, readonly=not mark_read)
        if status != "OK":
            log.error("SELECT failed: %s", data)
            conn.logout()
            return

        # UIDVALIDITY is in the untagged response items
        uidvalidity = None
        for item in data:
            if item and b"UIDVALIDITY" in (item if isinstance(item, bytes) else b""):
                try:
                    uidvalidity = item.decode().split()[-1]
                except Exception:
                    pass

        if uidvalidity:
            old_validity = state.get("uidvalidity", {}).get(folder)
            if old_validity and old_validity != uidvalidity:
                log.warning("UIDVALIDITY changed for %s — clearing processed UID cache", folder)
                state["processed_uids"] = [
                    u for u in state["processed_uids"]
                    if not u.startswith(f"{folder}:")
                ]
            if "uidvalidity" not in state:
                state["uidvalidity"] = {}
            state["uidvalidity"][folder] = uidvalidity
            save_state(state)

        # Fetch unseen messages
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            log.error("SEARCH failed: %s", data)
            conn.logout()
            return

        uid_list = data[0].split()
        log.info("Found %d unseen message(s) in %s", len(uid_list), folder)

        new_count = 0

        for uid_bytes in uid_list:
            uid = uid_bytes.decode()

            if already_processed(state, folder, uid):
                log.debug("Already processed UID %s — skipping", uid)
                continue

            status, raw_data = conn.fetch(uid_bytes, "(RFC822)")
            if status != "OK" or not raw_data or raw_data[0] is None:
                log.warning("Failed to fetch UID %s", uid)
                continue

            raw_msg = raw_data[0][1]
            msg     = email.message_from_bytes(raw_msg, policy=email.policy.compat32)

            subject  = str(msg.get("Subject", "No Subject")).strip()
            sender   = str(msg.get("From",    "Unknown")).strip()
            date_raw = msg.get("Date", "")

            try:
                date = email.utils.parsedate_to_datetime(date_raw)
            except Exception:
                date = datetime.now(tz=timezone.utc)

            log.info("Processing UID %s: %s", uid, subject)

            plain, html = get_bodies(msg)

            # Convert to Markdown — prefer HTML source for richer content
            body_md = html_to_md(html) if html else (plain or "*[Empty body]*")

            # Extract URLs from plain text + HTML-stripped text
            html_stripped = re.sub(r"<[^>]+>", " ", html) if html else ""
            all_urls  = extract_urls(plain + "\n" + html_stripped)

            # Fetch web content
            page_contents: dict[str, str] = {}
            if follow_urls:
                to_fetch = [u for u in all_urls if not should_skip(u, skip_domains)][:max_urls]
                for url in to_fetch:
                    log.info("  Fetching: %s", url)
                    content = fetch_page(url, timeout, max_chars)
                    page_contents[url] = content or ""

            # Write .md
            date_prefix = date.strftime("%Y-%m-%d")
            slug        = slugify(subject)
            filename    = template.format(date=date_prefix, slug=slug)
            out_path    = unique_path(out_dir, filename)

            md_text = build_md(subject, sender, date, body_md, page_contents)
            out_path.write_text(md_text, encoding="utf-8")
            log.info("  Written: %s", out_path)

            if mark_read:
                conn.store(uid_bytes, "+FLAGS", "\\Seen")

            mark_processed(state, folder, uid)
            new_count += 1

        conn.logout()
        log.info("Poll complete. Wrote %d new file(s).", new_count)

    except imaplib.IMAP4.error as exc:
        log.error("IMAP error: %s", exc)
    except ConnectionRefusedError:
        log.error("Connection refused to %s:%d", host, port)
    except OSError as exc:
        log.error("Network error: %s", exc)
    except Exception as exc:
        log.exception("Unexpected error: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Poll IMAP and convert emails to Markdown for Obsidian / LLM Wiki"
    )
    parser.add_argument("--setup",    action="store_true", help="Interactive first-time config")
    parser.add_argument("--daemon",   action="store_true", help="Run continuously on a timer")
    parser.add_argument("--interval", type=int, metavar="MINUTES",
                        help="Poll interval in minutes (overrides config)")
    parser.add_argument("--output",   type=Path, metavar="DIR",
                        help="Output directory for .md files (overrides config)")
    parser.add_argument("--debug",    action="store_true", help="Verbose debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.setup:
        interactive_setup()
        return

    cfg = load_config()

    if not cfg["imap"]["host"]:
        print("No config found. Run:  python3 mail_to_md.py --setup")
        sys.exit(1)

    if args.output:
        cfg["output"]["directory"] = str(args.output)

    interval = args.interval or int(cfg["daemon"]["interval_minutes"])
    state    = load_state()

    if args.daemon:
        log.info("Daemon mode — polling every %d minute(s). Ctrl-C to stop.", interval)
        while True:
            try:
                poll_once(cfg, state)
            except KeyboardInterrupt:
                log.info("Interrupted.")
                break
            except Exception as exc:
                log.exception("Poll failed: %s", exc)
            log.info("Next poll in %d minute(s).", interval)
            try:
                time.sleep(interval * 60)
            except KeyboardInterrupt:
                log.info("Interrupted.")
                break
    else:
        poll_once(cfg, state)


if __name__ == "__main__":
    main()
