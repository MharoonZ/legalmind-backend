# LegalMind platform overhaul — 27 November 2026

Covers both repositories (`legalmind-backend`, `legalmind-frontend`), branch
`claude/optimistic-lamport-pwbdbz`. The `/ask` API, retrieval and the AI
pipeline were not modified.

Contents: [1 Architecture](#1-existing-architecture) · [2 Problems found](#2-problems-found-and-what-changed) ·
[3 Folder structure](#3-folder-structure) · [4 Design system](#4-design-system) ·
[5 Performance](#5-performance) · [6 Database](#6-database-changes) · [7 API](#7-api) ·
[8 Workflows](#8-workflows) · [9 Password reset](#9-password-reset) ·
[10 Error handling](#10-error-handling) · [11 Security](#11-security-and-authorization) ·
[12 Tests](#12-test-results) · [13 Setup](#13-setup-and-migration) · [14 Limitations](#14-remaining-limitations)

## 1 Existing architecture

**Backend** — FastAPI, async SQLAlchemy 2, PostgreSQL (Supabase session pooler), Alembic.

- `app/api`, `app/qa`, `app/retrieval`, `app/embeddings`, `app/services`, `app/workers`: the legal corpus,
  retrieval and the `/ask` answer pipeline. Untouched.
- `app/platform`: accounts and all product features, mounted under `/platform`. One router-level dependency
  applies CSRF (custom header + allowed Origin) and a database-backed rate limit; one request = one
  transaction (`security.transaction`), committed only when the handler succeeds.
- Authentication: opaque session tokens, stored hashed, sent in an HttpOnly SameSite=Lax cookie; scrypt
  password hashes; roles `user`, `wakeel`, `admin`. Administrators are created only from the server CLI.
- Realtime: one authenticated WebSocket per tab; events are published with PostgreSQL `LISTEN/NOTIFY`
  inside the business transaction, so a rollback never emits an event.
- Email: a transactional outbox (`platform_mail_outbox`) drained by a background worker over SMTP.

**Frontend** — Next.js 16 App Router, React 19, no UI library.

- `src/app/page.tsx` is a Server Component. `src/app/[...path]/page.tsx` validates the route on the server and
  renders the client route table (`components/platform/pages.tsx`), which loads each screen on demand.
- `src/app/api/platform/[...path]` and `src/app/api/ask` are same-origin proxies; the browser never talks to
  the API host directly and never sees provider keys.
- `lib/platform.ts` is the typed API client; `lib/platform-cache.ts` is a per-user, in-memory request cache
  that deduplicates requests and is invalidated by mutations and realtime events.

## 2 Problems found and what changed

| Area | Finding | Change |
| --- | --- | --- |
| Hiring | No hiring step: a "preparation request" inside a chat was the only way to engage a Wakeel | Hiring requests and independent engagement records with a shared workspace |
| Cases | A case was a title, a description and a status the Wakeel changed freely | Proposal and approval, fee negotiation with history, confirmed payments, deadlines, per-case thread, completion, ratings |
| Engagements | Nothing represented the client–Wakeel relationship, so it could not be ended or rated | Reviewed termination that never closes matters silently; engagement rating; re-hiring creates a new record |
| Agreements | Only Wakeel-authored two-party service agreements | Multi-party agreements with emailed invitations, per-version approvals and signatures |
| Password reset | One error for incorrect/expired/used codes; expiry and limits hard-coded; a response difference could reveal whether an email was registered after the code expired; six-character minimum was the only password rule | Distinct, safe results; decoy challenges; configurable expiry/attempts/cooldown; shared password policy; dedicated resend endpoint |
| Errors (API) | Unhandled database errors became a bare 500 | Central handlers: integrity conflict 409, database unavailable 503, timeout 504 — never SQL or stack traces |
| Errors (UI) | Each page handled failures differently; several mutations failed silently (`.catch` into nothing) | One error model (`ApiError` + status mapping), toasts, inline alerts, error/empty/loading states, route error boundary |
| UI | A single 1,100-line stylesheet with one-off classes; inconsistent spacing and controls; top navigation with eleven links | Token-based design system, sidebar application shell, shared component kit |
| Performance | Detail endpoints issued 8–12 sequential statements; a dashboard needed several requests; read endpoints took row locks | Fewer statements per read, no locks on reads, one-request dashboard and badge counts (see section 5) |
| Documents | Files could only be attached to a case; type list was PDF/PNG/JPEG | Documents on hiring requests, workspaces and cases; titles, types, descriptions; `.docx`; one audited download route |
| Email | Plain template; OTP email could wait up to 30 s for the next worker poll | Branded responsive template; the worker is woken as soon as the transaction commits |

Preserved without behaviour changes: registration, login, logout, sessions, saved AI chats, guest chat,
conversations and messages, preparation requests, service agreements (drafts, versions, signing, amendments,
closure, disputes, expiry), case attachments, reviews, reports, administration, realtime updates.

## 3 Folder structure

```
legalmind-backend/app/platform
  models.py        all platform tables (new: hiring, relationships, case finance, ratings, parties)
  schemas.py       request validation, password policy, nested-payload validation helper
  security.py      sessions, CSRF, throttling, transaction dependency, notify()
  common.py        ownership checks, workspace activity, document storage helpers
  auth.py          register / login / profile / password / reset
  recovery.py      OTP request, resend and verification
  marketplace.py   Wakeel directory, conversations, messages, preparation requests
  hiring.py        hiring requests, engagements (workspaces), termination, engagement rating   [new]
  cases.py         cases: proposals, fees, payments, deadlines, thread, completion, rating
  agreements.py    service agreements; dispatches multi-party agreements
  multiparty.py    multi-party agreements, invitations, approvals, signatures                  [new]
  dashboard.py     dashboard, document library, calendar, account activity                     [new]
  admin.py         notifications, reviews, reports, administration, oversight lists
  mail.py          outbox, templates, SMTP worker
  workflows.py     agreement drafts, expiry and effective-date activation
  realtime.py      WebSocket hub
alembic/versions/20261127_23_engagements.py                                                   [new]
tests/test_engagements.py                                                                     [new]

legalmind-frontend/src
  app/layout.tsx, page.tsx, error.tsx, loading.tsx, not-found.tsx
  app/[...path]/page.tsx             server-side route validation and metadata
  app/styles/tokens.css              design tokens (light and dark)
  app/styles/system.css              component system
  app/styles/shell.css               sidebar layout and public header
  app/styles/{public,workspace,cases,agreements,account,assistant}.css   page-area styles
  app/legacy.css                     retained corpus tools, in a low-priority cascade layer
  components/platform/ui.tsx         component kit (buttons, forms, dialogs, tabs, toasts, states…)
  components/platform/icons.tsx      icon set and logo
  components/platform/shell.tsx      application chrome and navigation per role
  components/platform/pages.tsx      route table with on-demand loading
  components/platform/thread.tsx     message thread (general chat and case discussion)
  components/platform/documents-ui.tsx  document list, upload dialog, rating dialog
  components/platform/{dashboard,marketplace,hiring,workspace,messages}.tsx
  components/platform/{case-pages,documents,calendar}.tsx
  components/platform/{agreement-pages,agreement-wizard,multiparty}.tsx
  components/platform/{auth-pages,management,chat}.tsx
  lib/platform.ts                    typed API client, error normalisation, formatters
  lib/platform-cache.ts              per-user request cache
```

## 4 Design system

- **Identity**: warm paper neutrals, an ink-navy primary and a brass accent used only for small marks
  (ratings). A system serif is used for the marketing headline; everything else uses the system UI font, so no
  font files are downloaded and text never shifts.
- **Tokens** (`tokens.css`): colour roles (`--bg`, `--surface`, `--border`, `--text`, `--primary`, status
  colours with soft backgrounds), three radii, three shadows, three durations and one easing curve. Dark mode
  redefines the same tokens.
- **Components** (`system.css`, `ui.tsx`): Button, Badge/StatusBadge (one colour and wording per status across
  the product), Alert, Card, Stat, Tabs, Steps, Dialog, ConfirmDialog, Menu, Field, Form, PasswordField with
  strength meter, OtpInput, SearchInput, FileDrop, Avatar, Stars, KeyValue, Timeline, Skeleton, EmptyState,
  ErrorState, DataState, Toasts.
- **Layout**: signed-in pages use a 244 px sidebar grouped by task (workspace / records / account) with live
  badges; it becomes a slide-in drawer below 1000 px. Public pages use a compact top bar.
- **Responsive**: tables become stacked records on phones (each cell carries its column name), dialogs become
  bottom sheets, two-column forms collapse, inputs use 16 px text on phones to avoid zoom.
- **Motion**: CSS only, 120–260 ms. Page content rises in; dialogs, menus, toasts, tabs, the drawer, skeletons
  and message bubbles animate; nothing loops except loading indicators. `prefers-reduced-motion` disables it.
- **Accessibility**: visible focus rings, labelled icon buttons, `role="alert"`/`status` on feedback, keyboard
  support for tabs, menus and dialogs, a skip link.

## 5 Performance

PERF_SECTION

## 6 Database changes

Migration `20261127_23_engagements.py` (additive, forward-only). It was rehearsed on a local PostgreSQL 16:
a database at revision 22 with sample legacy rows was upgraded, its columns were compared with the schema the
models define (identical), and the new workflows were run against PostgreSQL.

New tables (all with row level security enabled and no public policies):

| Table | Purpose |
| --- | --- |
| `platform_hiring_requests` | Request to hire a Wakeel: category, requirements, description, communication, budget, status |
| `platform_relationships` | One engagement per accepted request; partial unique index allows one open engagement per pair and unlimited ended ones |
| `platform_case_fees` | Every fee proposal and its decision |
| `platform_case_payments` | Recorded payments and the Wakeel's confirmation |
| `platform_case_deadlines` | Hearings, meetings, filings and other dated items |
| `platform_ratings` | Case ratings and engagement ratings (one per author per subject) |
| `platform_agreement_parties` | Parties of a multi-party agreement: invitation, approval and signature state |

Changed tables: `platform_cases` (engagement, category, venue, fee, dates, completion columns),
`platform_case_updates` (`kind`, `from_status` — the status history), `platform_messages` (`case_id` for case
threads), `platform_attachments` (workspace or hiring-request parent, title, type, description, size),
`platform_agreements` (`kind`, `creator_id`, `relationship_id`, `category`, `reference`; three new statuses),
`platform_acceptances` (`evidence`), `platform_wakeels` (qualifications, services, fees),
`platform_otp_challenges` and `platform_mail_outbox` (nullable account for decoys and invitation emails).
New indexes: message thread order, unread notifications, audit by resource, case status and engagement.

Existing rows are kept. Backfills: agreement creator and category, attachment title and size, case
`updated_at`. Legacy cases (no engagement) keep their original status workflow.

## 7 API

All paths are under `/platform`. Mutations require the session cookie, `X-LegalMind-Client: web` and an
allowed Origin. Identities and roles always come from the session, never from the request body. List
endpoints return up to 200 rows and accept `offset`.

| Area | Endpoints |
| --- | --- |
| Password reset | `POST /auth/forgot-password`, `/auth/resend-otp`, `/auth/verify-otp`, `/auth/reset` |
| Hiring | `GET/POST /hiring-requests`, `GET /hiring-requests/{id}`, `POST …/actions` (discuss, accept, reject, cancel), `POST …/documents` |
| Engagements | `GET /relationships`, `GET /relationships/{id}`, `POST …/actions` (request, review, confirm, withdraw termination), `POST …/documents`, `POST …/rating` |
| Cases | `GET /cases?q=&status=&relationship_id=&sort=`, `POST /cases`, `GET /cases/{id}`, `POST …/actions`, `…/revise`, `…/updates`, `…/fees`, `…/fees/{id}/decision`, `…/payments`, `…/payments/{id}/decision`, `…/deadlines`, `…/deadlines/{id}`, `GET/POST …/messages`, `POST …/attachments`, `POST …/rating` |
| Agreements | `GET /agreements?q=&status=&kind=`, `POST /agreements` (service), `POST /agreements/multi-party`, `GET /agreements/{id}`, `POST /agreements/{id}/actions` |
| Invitations | `GET /agreement-invitations`, `POST /agreement-invitations/{id}/accept\|decline`, `POST /agreement-invitations/respond` (emailed token) |
| Workspace data | `GET /dashboard`, `GET /documents`, `GET /documents/{id}`, `GET /calendar`, `GET /activity` |
| Notifications | `GET /notifications`, `GET /notifications/count`, `POST /notifications/{id}/read`, `POST /notifications/read-all` |
| Administration | `GET /admin`, `GET /admin/records?kind=`, `GET /admin/settings`, `POST /admin/announcements`, `POST /admin/actions` |

Errors use `{"detail": "<safe sentence>", "code": "<optional machine code>"}`; validation errors use FastAPI's
field list. Exact request schemas are in `app/platform/schemas.py` and in `/docs` outside production.

## 8 Workflows

**Hiring** — `PENDING → UNDER_DISCUSSION → ACCEPTED | REJECTED | CANCELLED`. Only the client creates and
cancels; only the verified Wakeel discusses, accepts or rejects (rejection needs a reason). Acceptance creates
the engagement and its workspace. A pair can have one open request and one open engagement at a time.

**Cases** (inside an engagement) — either party proposes.

```
DRAFT → SUBMITTED → UNDER_DISCUSSION → ACCEPTED → ACTIVE → IN_PROGRESS → COMPLETION_REQUESTED → COMPLETED → CLOSED
          ↘ CHANGES_REQUESTED → (revised) SUBMITTED        ↔ ON_HOLD, AWAITING_CLIENT_INFORMATION, AWAITING_WAKEEL_ACTION,
          ↘ REJECTED / CANCELLED                              AWAITING_HEARING, HEARING_COMPLETED, AWAITING_DECISION
```

- The proposer can never approve their own proposal. Revising terms makes the reviser the proposer, so the
  other party must approve again.
- Fees: each proposal is stored; only the other party can accept or reject it; the agreed fee changes only on
  acceptance, including after the case is active. A case cannot become active without an agreed fee.
- Payments: a payment reported by the client is pending until the Wakeel confirms it; the payment status is
  derived from confirmed payments only.
- Completion: the Wakeel requests completion with a summary; the client confirms or raises a concern (the
  case returns to work). After completion the client can rate the case once.
- Every status, fee, payment, deadline and document event is written to the case timeline.

**Engagement termination** — `ACTIVE → TERMINATION_REQUESTED → UNDER_REVIEW → ENDED`. Either party requests
with a reason; the other reviews the checklist (open cases, pending payments, outstanding balances) and
confirms. Ending is refused while any case is open; during termination either party can close a case with a
recorded reason. If the other party does not respond for seven days, the requester can finish alone. Signed
agreements are not terminated. Afterwards the client can rate the engagement; hiring again creates a new
request, engagement and workspace, and the earlier history stays intact.

**Multi-party agreements**

```
DRAFT → SENT → UNDER_REVIEW → REVISION_REQUESTED → (revised) SENT → APPROVED → SIGNING_PENDING
      → PARTIALLY_SIGNED → FULLY_SIGNED → ACTIVE → COMPLETED | TERMINATED | EXPIRED      (DECLINED, WITHDRAWN)
```

- The creator lists parties by email. A creator who is not listed (for example a drafting Wakeel) is not a
  signatory and cannot sign.
- Invitations: an address that already has an account is matched and can accept in the app. An address
  without an account receives a link; the person must register with that address and open the link, which
  proves control of the mailbox. Nobody sees the agreement before accepting.
- Each revision is a new immutable version with a SHA-256 digest and resets approvals. Signatures are stored
  against the version number and digest and are never overwritten.
- Signing requires the session, the account password, the typed account name, explicit consent and the digest
  of the displayed version. The record keeps time, IP address, user agent, party and method.
- Completion and termination need a confirmation from every signatory.

Electronic signing note: the application records consent and evidence. It does not issue an advanced
electronic signature from an accredited certification service provider under Pakistan's Electronic
Transactions Ordinance, 2002, and that Ordinance excludes some instruments (for example negotiable
instruments, powers of attorney, trusts, wills and transfers of immovable property). The signing dialog says
this and makes no claim about legal validity; an operator should have counsel review the process before real
engagements rely on it.

## 9 Password reset

1. `POST /auth/forgot-password {email}` → always `200` with the same generic message, a challenge id, the
   expiry and the resend cooldown.
2. `POST /auth/verify-otp {email, challenge_id, otp}` → a reset token, or `otp_incorrect` (with attempts
   remaining), `otp_expired`, `otp_locked` (429) or `otp_invalid`.
3. `POST /auth/reset {token, password}` → password changed.

- The six-digit code is derived with HMAC-SHA-256 from `OTP_SECRET`, the challenge id and the account id. It
  is never stored or logged; the challenge row keeps only an HMAC digest.
- Expiry `OTP_EXPIRY_MINUTES` (5–10), `OTP_MAX_ATTEMPTS` wrong guesses, `OTP_RESEND_SECONDS` cooldown. A new
  code replaces the old one. Limits per IP and per address apply to requests and verifications.
- Unknown addresses get a decoy challenge with the same lifetime and attempt counting, so responses are
  identical for registered and unregistered emails.
- A verified code is consumed and exchanged for a separate hashed, single-use, ten-minute reset token.
- The new password must pass the shared policy (minimum length, not a common password, not the email or
  name, different from the current password) and is stored with the existing scrypt scheme.
- On success: every session, reset token and challenge of the account is revoked, the event is written to the
  audit log, and a confirmation email is queued. The confirmation cannot be disabled in preferences.

**SMTP configuration** (backend `.env` only): `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`,
`SMTP_FROM_EMAIL`, `SMTP_FROM_NAME`, `SMTP_USE_TLS` (true = STARTTLS on 587, false = implicit TLS on 465),
`FRONTEND_URL`, and `OTP_SECRET` (at least 32 characters, identical on every worker). Gmail needs an App
Password. Until these are set, recovery returns a clear 503 and other emails wait in the outbox.

## 10 Error handling

**Backend** — expected failures raise `HTTPException` with a sentence that is safe to display. Pydantic
validates every body (unknown fields are rejected); nested payloads validated inside a handler use
`schemas.parse`, which reports like any other 422. The request transaction rolls back as a whole on any
error, so a failed action cannot leave partial records (for example a case without its fee proposal, or
agreement parties without an agreement). Email is queued in the same transaction and sent afterwards, so an
SMTP failure never undoes a committed action; it is retried five times with backoff.

**Frontend** — `api()` turns every failure into an `ApiError` with `message`, `status`, `fields`, `code`:

| Situation | Result |
| --- | --- |
| Offline / connection refused | "Unable to connect. Check your internet connection and try again." |
| Timeout (30 s; 120 s for uploads) | "The request took too long. Please try again." |
| 400 / 409 / 403 / 404 / 429 | The API's own sentence; a fallback per status if none is safe |
| 401 outside the auth pages | Session cleared, private cache emptied, redirect to log in with `next` |
| 413 / 415 | File size / file type messages |
| 422 | Field messages under the fields plus a form message |
| 5xx | "We could not complete your request. Please try again later." |

`Form` shows field and form errors and blocks double submits; `useAction` does the same for buttons and
reports failures as toasts; `DataState` renders loading skeletons, an error panel with retry, and empty
states; `ConfirmDialog` guards consequential actions and can require a reason; `src/app/error.tsx` is the
route error boundary. Success messages are shown only after the API confirms.

## 11 Security and authorization

- Every read and write resolves the record through a membership query (`or_(user_id, wakeel_id)`, accepted
  party, or creator). Outsiders receive 404, not 403, so record ids cannot be probed.
- Role rules are enforced on the server: only clients hire; only the verified Wakeel accepts; proposers cannot
  self-approve; only the other party decides a fee; only the Wakeel confirms a payment; only the client rates;
  only signatories sign.
- Administrators see oversight metadata only (`/admin/records`): no descriptions, messages, terms or files.
  Each view is written to the audit log.
- Documents are stored in the database, never on a public path. Type is detected from file content, size is
  capped at 5 MB, names are sanitised, downloads are sent as attachments with a sandbox policy, and each
  download is audited.
- Audit: `platform_audit` is append-only in the application and records account, hiring, case, fee, payment,
  agreement, signature, termination, document and admin events. Case and agreement histories are kept in
  their own event tables.
- Supabase: all platform tables have RLS enabled with no policies, so they are unreachable through the
  public (anon/authenticated) API; the backend's database role is the only access path.

## 12 Test results

TEST_SECTION

## 13 Setup and migration

```bash
# Backend
pip install -r requirements.txt
cp .env.example .env            # set DATABASE_URL, SMTP_*, OTP_SECRET, FRONTEND_URL, CORS_ORIGINS
python -m alembic upgrade head  # applies 20261127_23; take a database backup first (forward-only)
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
python -m scripts.manage_accounts create-admin --email owner@example.com

# Frontend
npm ci
cp .env.example .env.local      # BACKEND_URL, NEXT_PUBLIC_WEBSOCKET_URL
npm run build && npm start
```

New or changed backend variables: `OTP_EXPIRY_MINUTES` (now 5–10), `OTP_MAX_ATTEMPTS` (default 5),
`OTP_RESEND_SECONDS` (default 60), `PASSWORD_MIN_LENGTH` (default 6). The frontend has no new variables.

Tests: `python -m pytest` (backend); `PLATFORM_TEST_DATABASE_URL=postgresql+asyncpg://…` runs the platform
tests on a disposable PostgreSQL. Browser journeys: see the frontend README.

## 14 Remaining limitations

LIMITATIONS_SECTION
