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
from app.platform.models import Account, Agreement, Case, Conversation, MailOutbox, OtpChallenge, now

logger = logging.getLogger(__name__)
SECURITY = {"otp", "security"}


def configured(settings):
    return bool(settings.smtp_host and settings.smtp_user and settings.smtp_password and settings.smtp_from_email)


def otp_value(identifier, user_id, settings=None):
    settings = settings or get_settings()
    value = hmac.new(settings.otp_secret.encode(), f"reset|{identifier}|{user_id}".encode(), hashlib.sha256).digest()
    return f"{int.from_bytes(value[:8], 'big') % 1000000:06d}"


def otp_digest(identifier, code, settings=None):
    settings = settings or get_settings()
    return hmac.new(settings.otp_secret.encode(), f"verify|{identifier}|{code}".encode(), hashlib.sha256).hexdigest()


async def enqueue(db, user_id, category, title, href="/dashboard", *, payload=None, batch=None):
    settings = get_settings()
    user = await db.get(Account, user_id)
    if not user or not user.active:
        return
    if category not in SECURITY and (
        not settings.email_notifications_enabled or not (user.email_preferences or {}).get(category, True)
    ):
        return
    identifier = (
        str(uuid5(NAMESPACE_URL, f"legalmind|{user_id}|{batch}|{int(time.time() // 300)}")) if batch else str(uuid4())
    )
    values = dict(
        id=identifier,
        user_id=user_id,
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


def render_message(user, row, settings, code=None):
    payload = row.payload
    title = payload["title"]
    href = payload.get("href", "/dashboard")
    if not href.startswith("/") or href.startswith("//") or "\\" in href:
        href = "/dashboard"
    url = settings.frontend_url.rstrip("/") + href
    lines = [f"Hello {user.name},"]
    if row.category == "otp":
        lines += [
            f"Your LegalMind password reset code is {code}.",
            "It expires five minutes after your request. Do not share this code. If you did not request it, ignore this email.",
        ]
    elif payload.get("welcome"):
        lines += [
            f"Welcome to LegalMind. Your {user.role} account is ready.",
            "Explore AI legal assistance, manage agreements and cases, and communicate securely with verified Wakeels.",
        ]
    else:
        lines += [title, payload.get("summary", "Open LegalMind to view the latest activity and details securely.")]
    text = "\n\n".join(lines) + "\n\n" + url
    paragraphs = "".join(f'<p style="line-height:1.6">{escape(line)}</p>' for line in lines)
    footer = (
        "Security messages cannot be disabled."
        if row.category in SECURITY
        else "Manage email preferences in your LegalMind profile."
    )
    html = f"""<!doctype html><html><body style="margin:0;background:#f2f5f3;color:#16382e;font-family:Arial,sans-serif">
    <div style="max-width:560px;margin:24px auto;background:white;padding:28px;border-radius:16px">
    <h1 style="font-size:25px">LegalMind</h1>{paragraphs}
    <a href="{escape(url, quote=True)}" style="display:inline-block;background:#164f3d;color:white;padding:13px 22px;border-radius:8px">Open LegalMind</a>
    <p style="font-size:12px;color:#576b63;margin-top:28px">{footer}</p></div></body></html>"""
    message = EmailMessage()
    message["Subject"] = title.replace("\r", "").replace("\n", "")
    message["From"] = formataddr((settings.smtp_from_name, settings.smtp_from_email))
    message["To"] = user.email
    message["Message-ID"] = f"<{row.id}@legalmind.notifications>"
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
            user = await db.get(Account, row.user_id)
            code = None
            allowed = (
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
                    if agreement:
                        resource = await db.get(Conversation, agreement.conversation_id)
                        if agreement.status == "DRAFT" and resource and user.id != resource.wakeel_id:
                            resource = None
                allowed = resource and user.id in {resource.user_id, resource.wakeel_id}
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
            message = render_message(user, row, self.settings, code)
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
