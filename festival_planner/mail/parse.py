"""Turn raw MIME bytes into a `ParsedEmail`.

Hostile or odd mail is the normal case here (Review Focus 2): `parse_message` is total. It never
raises on malformed input; whatever it cannot read becomes an empty or default field, so one bad
message cannot stop the poller. Only the standard library is used.
"""

from __future__ import annotations

import codecs
import contextlib
import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

DEFAULT_MAX_BODY = 200_000
TRUNCATED_MARKER = "\n[truncated]"
SUBJECT_MAX = 998  # RFC 5322 line limit; nothing sensible is longer

_MSG_ID_RE = re.compile(r"<([^<>\s]{1,998})>")
_TASK_TAG_RE = re.compile(r"\[T-(\d{1,18})\]")  # bounded: int() of a huge digit run raises
_GMAIL_MARKER_RE = re.compile(r"-{5,} ?Forwarded message ?-{5,}")
_OUTLOOK_RULE_RE = re.compile(r"_{10,}")
_FROM_LINE_RE = re.compile(r"From: (.+)")
_ADDRESS_RE = re.compile(r"[^\s<>\"'\[\]()]+@[^\s<>\"'\[\]()]+")
# How many lines after a Gmail marker can still be its header block.
_GMAIL_HEADER_LINES = 15

# Block tags end the current line; paragraph-level tags also leave a blank line.
_LINE_TAGS = frozenset(
    "address article aside br dd div dl dt fieldset figcaption figure footer form header hr li main"
    " nav section tbody td tfoot th thead tr".split()
)
_PARAGRAPH_TAGS = frozenset("blockquote body h1 h2 h3 h4 h5 h6 ol p pre table ul".split())
_SKIPPED_TAGS = frozenset({"script", "style", "head", "title", "template"})


@dataclass
class ParsedEmail:
    message_id: str
    thread_key: str
    from_addr: str
    to_addrs: list[str]
    cc_addrs: list[str]
    subject: str
    sent_at: str | None
    body_text: str
    attachment_names: list[str] = field(default_factory=list)
    forward_note: str | None = None
    forwarded_from: str | None = None
    task_tag: int | None = None


class _TextExtractor(HTMLParser):
    """HTML to text: drops script/style/head content, keeps line breaks for block tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._pre_depth = 0
        self._trail = 2  # newlines at the end of the output so far (2 = at a paragraph start)

    def _break(self, newlines: int) -> None:
        if self._trail < newlines:
            self._parts.append("\n" * (newlines - self._trail))
            self._trail = newlines

    def _tag_break(self, tag: str) -> None:
        if tag in _PARAGRAPH_TAGS:
            self._break(2)
        elif tag in _LINE_TAGS:
            self._break(1)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
        else:
            if tag == "pre":
                self._pre_depth += 1
            self._tag_break(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _SKIPPED_TAGS:
            self._tag_break(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        else:
            if tag == "pre":
                self._pre_depth = max(0, self._pre_depth - 1)
            self._tag_break(tag)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if not self._pre_depth:
            data = " ".join(data.split()) + (" " if data[-1:].isspace() and data.strip() else "")
            if not data or (data == " " and self._trail):
                return
            if self._trail and data.startswith(" "):
                data = data.lstrip()
        if not data:
            return
        self._parts.append(data)
        stripped = data.rstrip("\n")
        newlines = len(data) - len(stripped)
        self._trail = min(2, newlines) if stripped else min(2, self._trail + newlines)

    def text(self) -> str:
        return "".join(self._parts)


def _tidy(text: str) -> str:
    """Normalize newlines, drop NULs, trim line ends and collapse runs of blank lines."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t\f\v ]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    # Parser trouble must not lose what was already read.
    with contextlib.suppress(Exception):
        extractor.feed(html)
        extractor.close()
    return _tidy(extractor.text())


def _decode(part: Message) -> str:
    """Payload text: declared charset if usable, else UTF-8 with replacement characters."""
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None
    if not isinstance(payload, bytes):
        return ""
    try:
        charset = part.get_content_charset()
    except Exception:
        charset = None
    if charset:
        try:
            codecs.lookup(charset)
            return payload.decode(charset)
        except (LookupError, UnicodeError, ValueError):
            pass
    return payload.decode("utf-8", errors="replace")


def _clean(value: str) -> str:
    """Make a header string storable: undecodable raw bytes become replacement characters."""
    return value.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")


def _decode_words(value: str) -> str:
    """Decode RFC 2047 encoded words; on any trouble keep the cleaned text as is."""
    value = _clean(value)
    try:
        return _clean(str(make_header(decode_header(value))))
    except Exception:
        return value


def _header(msg: Message, name: str) -> str:
    try:
        value = msg.get(name)
        return " ".join(_clean(str(value)).split()) if value is not None else ""
    except Exception:
        return ""


def _addresses(msg: Message, name: str) -> list[str]:
    try:
        values = [str(v) for v in msg.get_all(name, [])]
        found = [addr.strip().lower() for _, addr in getaddresses(values) if "@" in addr]
    except Exception:
        return []
    return list(dict.fromkeys(found))  # de-duplicated, order kept


def _sent_at(msg: Message) -> str | None:
    value = _header(msg, "Date")
    if not value:
        return None
    try:
        moment = parsedate_to_datetime(value)
        if moment.tzinfo is not None:
            moment = moment.astimezone(UTC)
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # ValueError, TypeError, OverflowError on junk dates
        return None


def _message_id(msg: Message, raw: bytes) -> str:
    match = _MSG_ID_RE.search(_header(msg, "Message-ID"))
    if match:
        return f"<{match.group(1)}>"
    return f"<{hashlib.sha256(raw).hexdigest()}@generated.invalid>"


def _thread_key(msg: Message, message_id: str) -> str:
    for name in ("References", "In-Reply-To"):
        match = _MSG_ID_RE.search(_header(msg, name))
        if match:
            return f"<{match.group(1)}>"
    return message_id


def _walk(msg: Message):
    """Every leaf part, without descending into attached messages (message/rfc822)."""
    stack = [msg]
    while stack:
        part = stack.pop()
        if part.is_multipart() and part.get_content_type() != "message/rfc822":
            stack.extend(reversed(part.get_payload()))
        else:
            yield part


def _filename(part: Message) -> str | None:
    try:
        name = part.get_filename()
    except Exception:
        return None
    return " ".join(_decode_words(str(name)).split()) or None if name else None


def _collect(msg: Message) -> tuple[str, str, list[str]]:
    """Return (first text/plain body, first text/html body, attachment names)."""
    plain = html = ""
    names: list[str] = []
    for part in _walk(msg):
        ctype = part.get_content_type()
        name = _filename(part)
        disposition = _header(part, "Content-Disposition").split(";")[0].strip().lower()
        if name or disposition == "attachment" or ctype == "message/rfc822":
            names.append(name or ("message.eml" if ctype == "message/rfc822" else "attachment"))
        elif ctype == "text/plain" and not plain.strip():
            plain = _decode(part)
        elif ctype == "text/html" and not html.strip():
            html = _decode(part)
    return plain, html, names


def _cap(text: str, max_body: int) -> str:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= max_body:
        return text
    return data[: max(max_body, 0)].decode("utf-8", errors="ignore").rstrip() + TRUNCATED_MARKER


def _first_address(line: str) -> str | None:
    match = _ADDRESS_RE.search(line)
    if not match:
        return None
    return re.sub(r"(?i)^mailto:", "", match.group(0)).rstrip(".,;:").lower()


def _detect_forward(body: str) -> tuple[str | None, str | None]:
    """Return (forward_note, forwarded_from) for a Gmail- or Outlook-style forward."""
    lines = body.split("\n")
    offset = 0  # character offset of lines[i]
    for i, raw_line in enumerate(lines):
        line = raw_line.strip()
        sender: str | None = None
        if _GMAIL_MARKER_RE.fullmatch(line):
            for header in lines[i + 1 : i + 1 + _GMAIL_HEADER_LINES]:
                if not header.strip():
                    break
                m = _FROM_LINE_RE.fullmatch(header.strip())
                if m:
                    sender = _first_address(m.group(1))
                    break
        elif _OUTLOOK_RULE_RE.fullmatch(line):
            following = next((ln.strip() for ln in lines[i + 1 :] if ln.strip()), "")
            m = _FROM_LINE_RE.fullmatch(following)
            if not m:
                offset += len(raw_line) + 1
                continue
            sender = _first_address(m.group(1))
        else:
            offset += len(raw_line) + 1
            continue
        return (body[:offset].strip() or None), sender
    return None, None


def parse_message(raw: bytes, *, max_body: int = DEFAULT_MAX_BODY) -> ParsedEmail:
    """Parse raw RFC 5322 bytes. Never raises on malformed input."""
    msg = BytesParser(policy=policy.compat32).parsebytes(raw)

    message_id = _message_id(msg, raw)
    subject = _decode_words(_header(msg, "Subject"))[:SUBJECT_MAX]
    plain, html, attachment_names = _collect(msg)
    body = _tidy(plain)
    if not body and html:
        body = _html_to_text(html)
    body = _cap(body, max_body)
    # Detect on the capped text, but before the marker is added, so a forward marker is not
    # confused by it (the marker holds no forward syntax in any case).
    forward_note, forwarded_from = _detect_forward(body)

    tag = _TASK_TAG_RE.search(subject)
    return ParsedEmail(
        message_id=message_id,
        thread_key=_thread_key(msg, message_id),
        from_addr=(_addresses(msg, "From") or [""])[0],
        to_addrs=_addresses(msg, "To"),
        cc_addrs=_addresses(msg, "Cc"),
        subject=subject,
        sent_at=_sent_at(msg),
        body_text=body,
        attachment_names=attachment_names,
        forward_note=forward_note,
        forwarded_from=forwarded_from,
        task_tag=int(tag.group(1)) if tag else None,
    )
