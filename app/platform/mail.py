"""Transactional email outbox. SMTP runs outside request/DB transactions."""

import asyncio
import hashlib
import hmac
import logging
import smtplib
import ssl
import time
from datetime import UTC, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from html import escape
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import or_, select

from app.core.config import get_settings
from types import SimpleNamespace

from app.platform.models import (
    Account,
    Agreement,
    AgreementParty,
    Case,
    Conversation,
    MailOutbox,
    OtpChallenge,
    now,
)

logger = logging.getLogger(__name__)
SECURITY = {"otp", "security", "invite"}


def configured(settings):
    return bool(settings.smtp_host and settings.smtp_user and settings.smtp_password and settings.smtp_from_email)


def otp_value(identifier, user_id, settings=None):
    settings = settings or get_settings()
    value = hmac.new(settings.otp_secret.encode(), f"reset|{identifier}|{user_id}".encode(), hashlib.sha256).digest()
    return f"{int.from_bytes(value[:8], 'big') % 1000000:06d}"


def otp_digest(identifier, code, settings=None):
    settings = settings or get_settings()
    return hmac.new(settings.otp_secret.encode(), f"verify|{identifier}|{code}".encode(), hashlib.sha256).hexdigest()


async def enqueue(db, user_id, category, title, href="/dashboard", *, payload=None, batch=None, email=None):
    """Queue one email in the caller's transaction. `email` addresses a person without an account."""
    settings = get_settings()
    if user_id:
        user = await db.get(Account, user_id)
        if not user or not user.active:
            return
        if category not in SECURITY and (
            not settings.email_notifications_enabled or not (user.email_preferences or {}).get(category, True)
        ):
            return
    elif not email:
        return
    identifier = (
        str(uuid5(NAMESPACE_URL, f"legalmind|{user_id}|{batch}|{int(time.time() // 300)}")) if batch else str(uuid4())
    )
    values = dict(
        id=identifier,
        user_id=user_id,
        email=email,
        category=category,
        payload={"title": title[:180], "href": href, **(payload or {})},
        status="pending",
        attempts=0,
        created_at=now(),
        available_at=now() + timedelta(seconds=60 if batch else 0),
    )
    # A unique batch key prevents concurrent sends from creating duplicate alerts.
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    await db.execute(insert(MailOutbox).values(**values).on_conflict_do_nothing(index_elements=["id"]))
    db.info["mail_queued"] = True


BRAND = "#1f3a5f"


def layout(heading, blocks, button, url, footer, preheader=""):
    """Single-column, table-based layout that renders in common mail clients and on phones."""
    body = "".join(blocks)
    action = (
        f'''<table role="presentation" cellpadding="0" cellspacing="0" style="margin:28px 0 4px"><tr>
        <td style="background:{BRAND};border-radius:6px"><a href="{escape(url, quote=True)}"
        style="display:inline-block;padding:12px 22px;color:#ffffff;font-weight:600;font-size:15px;text-decoration:none">{escape(button)}</a></td>
        </tr></table>'''
        if button
        else ""
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(heading)}</title></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:#1c2430">
<span style="display:none;max-height:0;overflow:hidden;opacity:0">{escape(preheader)}</span>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border:1px solid #e3e6ea;border-radius:10px">
<tr><td style="padding:22px 28px;border-bottom:1px solid #e3e6ea;font-size:17px;font-weight:700;letter-spacing:.2px;color:{BRAND}">LegalMind</td></tr>
<tr><td style="padding:28px">
<h1 style="margin:0 0 16px;font-size:20px;line-height:1.35;color:#111827">{escape(heading)}</h1>
{body}{action}
</td></tr>
<tr><td style="padding:18px 28px;border-top:1px solid #e3e6ea;font-size:12px;line-height:1.6;color:#6b7280">{escape(footer)}<br>
This is an automated message from LegalMind. Please do not reply to this email.</td></tr>
</table></td></tr></table></body></html>"""


def paragraph(text):
    return f'<p style="margin:0 0 14px;font-size:15px;line-height:1.65;color:#374151">{escape(text)}</p>'


def render_message(user, row, settings, code=None, recipient=None):
    payload = row.payload
    title = payload["title"]
    href = payload.get("href", "/dashboard")
    if not href.startswith("/") or href.startswith("//") or "\\" in href:
        href = "/dashboard"
    url = settings.frontend_url.rstrip("/") + href
    name = user.name if user else payload.get("name", "")
    greeting = f"Hello {name}," if name else "Hello,"
    button = "Open LegalMind"
    lines = []
    extra = ""
    if row.category == "otp":
        minutes = settings.otp_expiry_minutes
        lines = [
            "Use this verification code to reset your LegalMind password.",
            f"The code expires in {minutes} minutes and can be used once. Never share it with anyone; "
            "LegalMind staff will never ask for it.",
            "If you did not request a password reset, you can ignore this email. Your password stays unchanged.",
        ]
        extra = (
            f'<p style="margin:6px 0 20px;padding:16px;background:#f3f6fa;border:1px solid #dbe3ec;border-radius:8px;'
            f"font-size:30px;font-weight:700;letter-spacing:8px;text-align:center;color:#111827;"
            f'font-family:SFMono-Regular,Consolas,Menlo,monospace">{escape(code or "")}</p>'
        )
        button = ""
    elif row.category == "invite":
        token = payload.get("token")
        url = settings.frontend_url.rstrip("/") + (
            "/agreements/invite#token=" + token if token else "/agreements/invitations"
        )
        lines = [
            f"{payload.get('inviter', 'A LegalMind member')} has invited you to take part in an agreement "
            f"as {payload.get('role', 'a party')}.",
            "For confidentiality, the agreement is only shown after you sign in to LegalMind with this email "
            "address and accept the invitation. If you do not have an account yet, you can create one first.",
        ]
        button = "Review invitation"
    elif payload.get("welcome"):
        lines = [
            f"Welcome to LegalMind. Your {'Wakeel' if user.role == 'wakeel' else 'client'} account is ready.",
            "Ask legal questions with cited sources, work with verified Wakeels, and manage cases and agreements in one place.",
        ]
    else:
        lines = [
            payload.get(
                "summary", "There is new activity on your LegalMind account. Sign in to view the details securely."
            )
        ]
    plain = [greeting, *([f"Verification code: {code}"] if code else []), *lines]
    text = "\n\n".join(plain) + ("\n\n" + url if button else "")
    footer = (
        "Security messages cannot be turned off."
        if row.category in SECURITY
        else "You can manage email notifications in your LegalMind settings."
    )
    html = layout(
        title, [paragraph(greeting), extra, *[paragraph(line) for line in lines]], button, url, footer, lines[0]
    )
    message = EmailMessage()
    message["Subject"] = title.replace("\r", "").replace("\n", "")
    message["From"] = formataddr((settings.smtp_from_name, settings.smtp_from_email))
    message["To"] = recipient or user.email
    message["Message-ID"] = f"<{row.id}@legalmind.notifications>"
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


def deliver(message, settings):
    context = ssl.create_default_context()
    if settings.smtp_use_tls:
        client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10)
    else:
        # false means implicit TLS (normally port 465), never plaintext auth.
        client = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=10, context=context)
    with client:
        if settings.smtp_use_tls:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        client.login(settings.smtp_user, settings.smtp_password)
        client.send_message(message)


class MailWorker:
    def __init__(self, database, hub, settings):
        self.db, self.hub, self.settings = database, hub, settings
        self.task = None
        self.wake = asyncio.Event()

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def run(self):
        while True:
            try:
                from app.platform.workflows import expire_agreements

                await expire_agreements(self.db, self.hub)
                if configured(self.settings):
                    for _ in range(20):
                        if not await self.send_one():
                            break
            except Exception as exc:
                # Never log SMTP exception bodies (some servers include addresses).
                logger.warning("mail_worker_failed error=%s", type(exc).__name__)
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), 30)
            except TimeoutError:
                pass

    async def send_one(self):
        async with self.db.session() as db, db.begin():
            row = await db.scalar(
                select(MailOutbox)
                .where(
                    MailOutbox.available_at <= now(),
                    or_(
                        MailOutbox.status == "pending",
                        (MailOutbox.status == "sending") & (MailOutbox.claimed_at < now() - timedelta(minutes=5)),
                    ),
                )
                .order_by(MailOutbox.available_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if not row:
                return False
            user = await db.get(Account, row.user_id) if row.user_id else None
            code = None
            allowed = bool(row.email) or (
                user
                and user.active
                and (
                    row.category in SECURITY
                    or (
                        self.settings.email_notifications_enabled
                        and (user.email_preferences or {}).get(row.category, True)
                    )
                )
            )
            # Recheck access at delivery time; assignments may have changed while queued.
            href = row.payload.get("href", "")
            parts = href.strip("/").split("/")
            if allowed and len(parts) == 2 and parts[0] in {"cases", "agreements", "messages"}:
                resource = None
                if parts[0] == "cases":
                    resource = await db.get(Case, parts[1])
                elif parts[0] == "messages":
                    resource = await db.get(Conversation, parts[1])
                else:
                    agreement = await db.get(Agreement, parts[1])
                    if agreement and agreement.kind == "multi_party":
                        party = await db.scalar(
                            select(AgreementParty.id).where(
                                AgreementParty.agreement_id == agreement.id,
                                AgreementParty.account_id == user.id,
                                AgreementParty.invite_status == "ACCEPTED",
                            )
                        )
                        resource = (
                            SimpleNamespace(user_id=user.id, wakeel_id=None)
                            if (party or agreement.creator_id == user.id)
                            else None
                        )
                    elif agreement:
                        resource = await db.get(Conversation, agreement.conversation_id)
                        if agreement.status == "DRAFT" and resource and user.id != resource.wakeel_id:
                            resource = None
                allowed = resource and user.id in {resource.user_id, resource.wakeel_id}
            if row.category == "invite":
                # An invitation that was answered or withdrawn is no longer delivered.
                party = await db.get(AgreementParty, row.payload.get("party_id", ""))
                allowed = bool(party and party.invite_status == "PENDING" and party.invite_token_hash)
            if row.category == "otp":
                challenge = await db.get(OtpChallenge, row.payload.get("challenge_id"))
                allowed = (
                    allowed and challenge and not challenge.used and challenge.expires_at.replace(tzinfo=UTC) > now()
                )
                if allowed:
                    code = otp_value(challenge.id, user.id, self.settings)
            if not allowed:
                row.status = "skipped"
                row.payload = {}
                return True
            row.status, row.claimed_at = "sending", now()
            row.attempts += 1
            attempt, identifier = row.attempts, row.id
            message = render_message(user, row, self.settings, code, recipient=row.email)
        failed = False
        try:
            await asyncio.to_thread(deliver, message, self.settings)
        except Exception as exc:
            failed = True
            logger.warning("mail_delivery_failed id=%s error=%s", identifier, type(exc).__name__)
        async with self.db.session() as db, db.begin():
            row = await db.get(MailOutbox, identifier)
            row.status = ("failed" if attempt >= 5 else "pending") if failed else "sent"
            row.available_at = now() + timedelta(seconds=min(3600, 30 * 2**attempt))
            if not failed:
                row.payload = {}  # Do not retain delivered private message data.
        return True
