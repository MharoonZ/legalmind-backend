# Workflow and email enhancement report — 26 November 2026

Branch: `feature/workflow-email-enhancements` in both repositories.

## Behavior and implementation

- `app/platform/mail.py`: reusable HTML/plain-text templates for welcome, message, agreement, case and security emails. Database outbox commits with the business operation; SMTP runs in a background thread. STARTTLS or implicit TLS validates certificates. Five attempts with exponential backoff, stale-claim recovery, stable Message-ID and per-conversation/recipient five-minute batching. Private message bodies and documents are omitted from emails. Preferences and current record permissions are checked again before delivery.
- `recovery.py`, `auth.py`, `schemas.py`: request code → verify six-digit code → reset password. OTP expires 300 seconds after creation; resend cooldown 60 seconds; maximum five incorrect guesses; shared IP/account rate limits. HMAC-protected codes are never stored as plaintext, including in the outbox. Reset tokens are hashed, single-use and valid for five minutes. Passwords require six characters, use scrypt, and resets revoke all sessions and other recovery challenges. Security confirmation emails bypass optional preferences.
- `workflows.py`, `agreements.py`: private, revision-checked resumable drafts; preparation decisions; immutable agreement versions; mutual termination; terminal-state archive. Signed end dates expire eligible agreements after the end date in Asia/Karachi. Disputed agreements remain under explicit dispute resolution. Expiry is a system action, checked approximately every 30 seconds while an API worker is running.
- `cases.py`: independent owner/assigned-Wakeel permissions, controlled status transitions, assignment audit, dates, documents and activity. Optional many-to-many agreement links require identical parties and access to both records. Private agreement drafts remain hidden from clients. Linked cases cannot be reassigned to a different Wakeel; open a separate case to preserve confidential history. Ending an agreement never changes case status, and closing a case never changes an agreement.
- `security.py`, `events.py` and existing PostgreSQL realtime broker: transaction-bound invalidations for both participants, related records, drafts, notification counts and preferences. Reconnecting clients reload current data. Revoked-access browser caches clear protected records on 401/403/404.

## Database and deployment

Migration `20261126_22_workflows_mail.py` adds email preferences, signed expiry dates, explicit case parties, draft context, OTP challenges, outbox and case/agreement junction. Existing case ownership and links are backfilled. New private tables enable RLS with no public policies. Relationship, expiry and queue indexes support the new queries.

The migration was rehearsed against live Supabase inside a rolled-back transaction, then applied successfully. No test identities or fixture cases were inserted into Supabase. This migration is forward-only because independent cases cannot be represented by the old required-agreement schema without data loss; use a verified backup for a rollback.

For another environment: deploy backend and frontend from the matching branches and run `python -m alembic upgrade head` before starting the new backend.

## Backend environment

See `.env.example`: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`, `SMTP_FROM_NAME`, `SMTP_USE_TLS`, `FRONTEND_URL`, `OTP_SECRET`, `OTP_EXPIRY_MINUTES=5`, `OTP_LENGTH=6`, `EMAIL_NOTIFICATIONS_ENABLED`.

Configure SMTP credentials locally (Gmail requires an App Password). Generate a stable OTP secret of at least 32 characters with `python -c "import secrets; print(secrets.token_urlsafe(48))"`; use the same secret on all workers. Set the actual HTTPS frontend URL in production, then restart the backend. No SMTP secrets belong in frontend environment variables.

Recovery returns a clear 503 until SMTP and OTP_SECRET are configured. Welcome/activity emails stay queued while SMTP is unavailable. Monitor failed outbox records; retries stop after five attempts. SMTP has at-least-once semantics: a crash after server acceptance but before recording success can cause a duplicate despite stable message IDs. This change implements password recovery, not registration email-ownership verification. Previously exposed credentials still require rotation by their project owner.

## Added API surface

All paths below start with `/platform`:

- POST `/auth/forgot-password`, `/auth/verify-otp`; existing POST `/auth/reset` accepts the verified recovery token.
- GET/POST `/auth/email-preferences`.
- GET `/agreement-drafts`; GET/POST `/agreement-drafts/{conversation}`.
- POST `/preparations/{id}/actions` (accept/decline).
- POST `/agreements/{id}/actions` adds request_termination, confirm_termination, archive.
- POST `/cases` supports an independent case; POST `/cases/{id}/assignment` and `/cases/{id}/agreements` assign/link.
- GET `/notifications/count` returns authoritative unread count.

Frontend adds a four-step agreement wizard, saved-draft restoration, review and version comparison, case tabs and reciprocal links, OTP screens with resend cooldown and password confirmation, email preferences, unread badge, breadcrumbs, collapsible Wakeel discovery and shared-theme styles. See the frontend README for its file map.

## Focused verification

Eight new backend regressions passed: OTP success/reuse, expiry, lockout, resend, mail retry/batching/preferences, case permissions/drafts/links, expiry and termination independence. Existing complete workflow, one-use reset and WebSocket privacy/revocation checks also passed. The WebSocket test had one test-client cancellation during teardown in the combined run; its isolated rerun passed.

Two focused Chromium workflows passed: draft save/reload/review and real-time linking with a private draft, including mobile dark layout; and OTP screens/password-confirmation errors (mocked email endpoints). Backend OTP behavior uses the real HTTP routes with an isolated database. SMTP delivery uses a mocked transport; no real mail was sent.

Frontend production build/type checking, changed-area ESLint and backend Ruff passed. Next.js security check reports no update needed for 16.4.6. The broad test suites and full responsive matrix were deliberately not rerun, per the focused-testing request. Actual inbox delivery remains a manual check after SMTP configuration.
