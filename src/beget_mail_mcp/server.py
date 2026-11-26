from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import imaplib
import json
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


MAX_RESULTS = 50
MAX_BODY_CHARS = 100_000
MAX_SEND_BODY_CHARS = 100_000
CONFIRMATION_TTL_SECONDS = 10 * 60


@dataclass(frozen=True)
class Settings:
    email: str
    password: str
    bearer_token: str
    imap_host: str = "imap.beget.com"
    imap_port: int = 993
    smtp_host: str = "smtp.beget.com"
    smtp_port: int = 465
    mailbox: str = "INBOX"
    host: str = "0.0.0.0"
    port: int = 8000

    @classmethod
    def from_env(cls) -> "Settings":
        missing = [
            name
            for name in ("BEGET_EMAIL", "BEGET_PASSWORD", "MCP_BEARER_TOKEN")
            if not os.getenv(name)
        ]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        token = os.environ["MCP_BEARER_TOKEN"]
        if len(token) < 32:
            raise RuntimeError("MCP_BEARER_TOKEN must contain at least 32 characters")
        return cls(
            email=os.environ["BEGET_EMAIL"],
            password=os.environ["BEGET_PASSWORD"],
            bearer_token=token,
            imap_host=os.getenv("BEGET_IMAP_HOST", "imap.beget.com"),
            imap_port=int(os.getenv("BEGET_IMAP_PORT", "993")),
            smtp_host=os.getenv("BEGET_SMTP_HOST", "smtp.beget.com"),
            smtp_port=int(os.getenv("BEGET_SMTP_PORT", "465")),
            mailbox=os.getenv("BEGET_MAILBOX", "INBOX"),
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
        )


class EmailSummary(BaseModel):
    uid: str
    subject: str
    sender: str
    recipients: list[str]
    date: str | None = None
    size_bytes: int | None = None


class EmailList(BaseModel):
    mailbox: str
    emails: list[EmailSummary]


class EmailDetail(EmailSummary):
    text: str
    attachment_names: list[str]


class PreparedEmail(BaseModel):
    from_address: str
    to: list[str]
    cc: list[str]
    subject: str
    text: str
    confirmation_token: str
    expires_in_seconds: int


class SendResult(BaseModel):
    sent: bool
    from_address: str
    to: list[str]
    cc: list[str]
    subject: str


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeDecodeError):
        return value


def _date_iso(message: Message) -> str | None:
    raw = message.get("Date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).isoformat()
    except (TypeError, ValueError, OverflowError):
        return raw


def _message_text(message: Message) -> str:
    body = message.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    try:
        content = body.get_content()
    except (LookupError, UnicodeDecodeError):
        payload = body.get_payload(decode=True) or b""
        content = payload.decode("utf-8", errors="replace")
    if body.get_content_type() == "text/html":
        content = re.sub(r"<[^>]+>", " ", content)
        content = html.unescape(content)
        content = re.sub(r"\s+", " ", content).strip()
    return content[:MAX_BODY_CHARS]


def _attachment_names(message: Message) -> list[str]:
    names: list[str] = []
    for part in message.walk():
        filename = part.get_filename()
        if filename:
            names.append(_decode_header(filename))
    return names


def _extract_message_bytes(fetch_data: list[Any]) -> bytes:
    for item in fetch_data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    raise RuntimeError("Beget IMAP returned an unexpected message payload")


def _summary(uid: str, raw: bytes, size: int | None = None) -> EmailSummary:
    message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    return EmailSummary(
        uid=uid,
        subject=_decode_header(message.get("Subject")) or "(без темы)",
        sender=_decode_header(message.get("From")),
        recipients=[address for _, address in getaddresses(message.get_all("To", []))],
        date=_date_iso(message),
        size_bytes=size,
    )


class MailClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _imap(self) -> imaplib.IMAP4_SSL:
        connection = imaplib.IMAP4_SSL(
            self.settings.imap_host,
            self.settings.imap_port,
            ssl_context=ssl.create_default_context(),
        )
        connection.login(self.settings.email, self.settings.password)
        status, _ = connection.select(self.settings.mailbox, readonly=True)
        if status != "OK":
            connection.logout()
            raise RuntimeError(f"Cannot open mailbox {self.settings.mailbox}")
        return connection

    def list_emails(self, limit: int) -> EmailList:
        connection = self._imap()
        try:
            status, data = connection.uid("search", None, "ALL")
            if status != "OK":
                raise RuntimeError("IMAP list operation failed")
            uids = (data[0] or b"").split()[-limit:][::-1]
            emails = [self._fetch_summary(connection, uid.decode("ascii")) for uid in uids]
            return EmailList(mailbox=self.settings.mailbox, emails=emails)
        finally:
            connection.logout()

    def search_emails(self, query: str, limit: int) -> EmailList:
        connection = self._imap()
        try:
            query_bytes = query.encode("utf-8")
            status, data = connection.uid("search", "CHARSET", "UTF-8", "TEXT", query_bytes)
            if status != "OK" and query.isascii():
                status, data = connection.uid("search", None, "TEXT", query)
            if status != "OK":
                raise RuntimeError("Beget IMAP did not accept the search query")
            uids = (data[0] or b"").split()[-limit:][::-1]
            emails = [self._fetch_summary(connection, uid.decode("ascii")) for uid in uids]
            return EmailList(mailbox=self.settings.mailbox, emails=emails)
        finally:
            connection.logout()

    def _fetch_summary(self, connection: imaplib.IMAP4_SSL, uid: str) -> EmailSummary:
        status, data = connection.uid(
            "fetch", uid, "(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT)] RFC822.SIZE)"
        )
        if status != "OK":
            raise RuntimeError(f"Cannot read email UID {uid}")
        raw = _extract_message_bytes(data)
        size_match = re.search(rb"RFC822\.SIZE (\d+)", data[0][0] if data and isinstance(data[0], tuple) else b"")
        size = int(size_match.group(1)) if size_match else None
        return _summary(uid, raw, size)

    def get_email(self, uid: str) -> EmailDetail:
        if not uid.isdigit():
            raise ValueError("uid must contain digits only")
        connection = self._imap()
        try:
            status, data = connection.uid("fetch", uid, "(BODY.PEEK[] RFC822.SIZE)")
            if status != "OK" or not data or data[0] is None:
                raise ValueError(f"Email UID {uid} was not found")
            raw = _extract_message_bytes(data)
            message = BytesParser(policy=policy.default).parsebytes(raw)
            basic = _summary(uid, raw, len(raw))
            return EmailDetail(
                **basic.model_dump(),
                text=_message_text(message),
                attachment_names=_attachment_names(message),
            )
        finally:
            connection.logout()

    def send(self, to: list[str], cc: list[str], subject: str, text: str) -> SendResult:
        message = EmailMessage()
        message["From"] = self.settings.email
        message["To"] = ", ".join(to)
        if cc:
            message["Cc"] = ", ".join(cc)
        message["Subject"] = subject
        message.set_content(text)
        with smtplib.SMTP_SSL(
            self.settings.smtp_host,
            self.settings.smtp_port,
            context=ssl.create_default_context(),
        ) as smtp:
            smtp.login(self.settings.email, self.settings.password)
            smtp.send_message(message)
        return SendResult(
            sent=True,
            from_address=self.settings.email,
            to=to,
            cc=cc,
            subject=subject,
        )


def _validate_addresses(values: list[str]) -> list[str]:
    if len(values) > 20:
        raise ValueError("At most 20 recipients are allowed")
    result: list[str] = []
    for value in values:
        if "\r" in value or "\n" in value:
            raise ValueError("Recipient addresses cannot contain line breaks")
        parsed = getaddresses([value])
        if len(parsed) != 1 or not parsed[0][1] or "@" not in parsed[0][1]:
            raise ValueError(f"Invalid email address: {value}")
        result.append(parsed[0][1])
    return result


def _email_payload(to: list[str], cc: list[str], subject: str, text: str, expires: int) -> bytes:
    return json.dumps(
        {"to": to, "cc": cc, "subject": subject, "text": text, "expires": expires},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _confirmation_key(settings: Settings) -> bytes:
    return hashlib.sha256(("beget-mail-confirmation:" + settings.bearer_token).encode()).digest()


def _make_confirmation_token(
    settings: Settings, to: list[str], cc: list[str], subject: str, text: str
) -> str:
    expires = int(time.time()) + CONFIRMATION_TTL_SECONDS
    payload = _email_payload(to, cc, subject, text, expires)
    signature = hmac.new(_confirmation_key(settings), payload, hashlib.sha256).digest()
    payload_part = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature_part = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{payload_part}.{signature_part}"


def _verify_confirmation_token(
    settings: Settings,
    token: str,
    to: list[str],
    cc: list[str],
    subject: str,
    text: str,
) -> None:
    try:
        payload_part, signature_part = token.split(".", 1)
        payload = base64.urlsafe_b64decode(payload_part + "=" * (-len(payload_part) % 4))
        signature = base64.urlsafe_b64decode(
            signature_part + "=" * (-len(signature_part) % 4)
        )
        expected = hmac.new(_confirmation_key(settings), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        stored = json.loads(payload)
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ValueError("Invalid confirmation token") from exc
    expected_payload = {
        "to": to,
        "cc": cc,
        "subject": subject,
        "text": text,
        "expires": stored.get("expires"),
    }
    if stored != expected_payload:
        raise ValueError("Email changed after it was prepared; prepare it again")
    if not isinstance(stored["expires"], int) or stored["expires"] < int(time.time()):
        raise ValueError("Confirmation token expired; prepare the email again")


def _validate_email_content(
    to: list[str], cc: list[str] | None, subject: str, text: str
) -> tuple[list[str], list[str]]:
    cc = cc or []
    clean_to = _validate_addresses(to)
    clean_cc = _validate_addresses(cc)
    if not clean_to:
        raise ValueError("At least one recipient is required")
    if "\r" in subject or "\n" in subject:
        raise ValueError("Subject cannot contain line breaks")
    if len(subject) > 998:
        raise ValueError("Subject is too long")
    if len(text) > MAX_SEND_BODY_CHARS:
        raise ValueError(f"Message body exceeds {MAX_SEND_BODY_CHARS} characters")
    return clean_to, clean_cc


class BearerTokenMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, token: str):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next: Any):
        if request.url.path == "/health" or request.method == "OPTIONS":
            return await call_next(request)
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {self.token}"
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


settings = Settings.from_env()
mail = MailClient(settings)
mcp = FastMCP(
    "Beget Mail",
    instructions=(
        "Read and search one authenticated Beget mailbox. Never send an email until the user "
        "has reviewed the exact recipients, subject, and body returned by prepare_email."
    ),
)


@mcp.tool(
    title="List recent emails",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
)
def list_recent_emails(
    limit: int = Field(default=10, ge=1, le=MAX_RESULTS),
) -> EmailList:
    """List the newest messages in the configured mailbox without marking them as read."""
    return mail.list_emails(limit)


@mcp.tool(
    title="Search emails",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
)
def search_emails(
    query: str = Field(min_length=1, max_length=500),
    limit: int = Field(default=10, ge=1, le=MAX_RESULTS),
) -> EmailList:
    """Search the configured mailbox by text in message headers or body."""
    return mail.search_emails(query, limit)


@mcp.tool(
    title="Read email",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
)
def get_email(uid: str = Field(pattern=r"^\d+$")) -> EmailDetail:
    """Read one email by the stable IMAP UID returned by list or search."""
    return mail.get_email(uid)


@mcp.tool(
    title="Prepare email",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True),
)
def prepare_email(
    to: list[str],
    subject: str,
    text: str,
    cc: list[str] | None = None,
) -> PreparedEmail:
    """Prepare an exact email preview and short-lived token; this does not send anything."""
    clean_to, clean_cc = _validate_email_content(to, cc, subject, text)
    token = _make_confirmation_token(settings, clean_to, clean_cc, subject, text)
    return PreparedEmail(
        from_address=settings.email,
        to=clean_to,
        cc=clean_cc,
        subject=subject,
        text=text,
        confirmation_token=token,
        expires_in_seconds=CONFIRMATION_TTL_SECONDS,
    )


@mcp.tool(
    title="Send email",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
)
def send_email(
    to: list[str],
    subject: str,
    text: str,
    confirmation_token: str,
    cc: list[str] | None = None,
) -> SendResult:
    """Send the exact email previously returned by prepare_email after explicit user approval."""
    clean_to, clean_cc = _validate_email_content(to, cc, subject, text)
    _verify_confirmation_token(
        settings, confirmation_token, clean_to, clean_cc, subject, text
    )
    return mail.send(clean_to, clean_cc, subject, text)


app = mcp.streamable_http_app()
app.add_middleware(BearerTokenMiddleware, token=settings.bearer_token)


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


app.add_route("/health", health, methods=["GET"])


def main() -> None:
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()

