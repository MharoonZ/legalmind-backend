# Platform implementation

## Scope and sequence

The starting backend is feature/ask at b1709e1 and the frontend is shimza at
dd88144. The live database started at 20261111_19 and is now at 20261126_20. Existing corpus records,
PDF ingestion, retrieval and cited answers are preserved.

1. Inspect existing repositories, source contracts, settings and live database.
2. Add private platform models, an additive migration and server sessions.
3. Add saved chats around the existing QA entry point.
4. Add discovery, messaging, versioned service agreements, cases and administration.
5. Build public navigation, role dashboards and responsive feature pages.
6. Test access control, transactions, workflow transitions, navigation and responsive layouts.
7. Validate the migration, apply it to the configured database, run a live check and push both branches.

## Data and authentication

New tables use the platform_ prefix. Existing app_users are corpus operator
identities and the existing Supabase Auth user is preserved. Platform accounts
are independent and use scrypt password hashes. Public registration accepts
user or wakeel. Administrator creation requires a backend terminal command.
Only verified, active Wakeels appear in discovery.

Opaque random session tokens are hashed in the database and sent in HttpOnly,
SameSite=Lax cookies. Production cookies require HTTPS. Sessions expire after
seven idle days and thirty absolute days. Refresh extends the idle period.
Logout revokes the current token. Password changes revoke all existing sessions.
CSRF protection requires an allowed Origin when present and a custom header.
A database-backed request counter and stricter login counters span API workers.
Frontend requests use a same-origin Next.js proxy and never receive provider keys.

Every private resource checks membership on the backend. New PostgreSQL tables
enable RLS with no public client policies. The backend database role remains the
trusted access layer. Browser clients use the FastAPI API.

## Agreements

Preparation requests and actual agreements are separate. A verified Wakeel
creates the agreement after messaging with the client. Complete immutable terms
are stored per version, with a SHA-256 digest. Requests specify the current
revision and mutations lock the agreement to reject stale or concurrent actions.

The client reviews, declines with a reason, requests changes or accepts.
Acceptance opens signing. Both parties separately enter their account name,
confirm consent and sign the displayed version digest. Activation requires both
signatures. Amendments retain the active version until both parties sign the
replacement. Closure requires confirmations from both participants. Disputes
enter administrator review. An administrator may reopen the existing signed
agreement or cancel it with an audited reason.

The recorded acceptance is an application consent record. Identity assurance
and the legal suitability of this acceptance process require operator review
before real legal engagements.

## Operations

Apply migrations with `python -m alembic upgrade head`. Run the backend with
`python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`. Run the frontend
with `npm install`, `npm run build`, and `npm start`.

Create an admin with
`python -m scripts.manage_accounts create-admin --email owner@example.com`.
The command prompts privately for the password. Public signup cannot create admins.

Password recovery is operator-assisted until an email delivery service is
configured. After verifying the owner, run
`python -m scripts.manage_accounts reset-link --email user@example.com`.
Deliver the one-use, thirty-minute link privately. The reset page consumes its
token and revokes sessions. Tokens are never printed by API requests.

Case attachments are private, limited to 5 MB and downloaded as attachments.
Only PDF, PNG and JPEG signatures are accepted. Deploy an antivirus scanner
before accepting untrusted real-world documents at scale. Back up the database,
including attachment bytes and agreement history.

Use HTTPS and a validated database CA in production. Set CORS_ORIGINS to the
frontend origin. API_KEYS, if enabled for the existing public /ask API, should
be configured only in the server-side frontend proxy. The legacy EMBEDDING_MODEL
and EMBEDDING_DIM values do not select the active publication's embedding model.
The current publication uses its pinned BGE profile.

## Verification

Backend full suite: **734 passed, 5 skipped**. The fourteen new platform/security
cases cover ownership, CSRF, expiry/reset, revocation, contract transitions,
immutable versions, signatures, amendments, closure/disputes, private documents,
notifications and review eligibility. The skipped tests are environment-dependent.

The migration was exercised in an isolated PostgreSQL schema and rolled back,
then successfully applied to live Supabase. All twenty-one new tables have RLS
enabled. The live database retained 23 laws, 531 chunks, two corpus identities and
its existing Supabase Auth user. No demonstration platform accounts were inserted.
Live startup checks returned 200 for empty public Wakeel discovery, 401 for
unauthenticated profile access, and 200/answered with citations from existing
/ask for Section 3 of the Punjab Prevention of Gambling Ordinance.

All three new Playwright scenarios passed against real FastAPI routes with a
temporary SQLite database, fixture corpus and scripted model. They cover guest
transfer, saved history, logout/back navigation, messaging, preparation,
negotiation, signing, completion, reviews, cases, registration and admin access.
Desktop/tablet/mobile checks passed at 1440, 768 and 390 pixels across public and
auth pages, AI, both participant dashboards, profiles, messaging, agreement forms
and history, case forms/details, notifications, settings and administration.
These tests use isolated test identities, not fake records in the live product.

Frontend TypeScript, ESLint and the Next.js 16.4.6 production build passed.
All 23 retained QA/ingestion/inspection browser regressions passed. Backend Ruff
checks passed. Dependency updates left npm audit with zero reported vulnerabilities
at verification time. A fresh read-only live check reconfirmed migration 20261126_20,
21 tables with RLS enabled and zero platform accounts immediately before delivery.

## Frontend and chat behavior

The public homepage links to /ask, discovery and separate auth pages. /dashboard
shows role-specific actions; /profile and /settings support profile/password
changes. /wakeels, /messages, /agreements, /cases, /notifications and /admin use
real backend records. /inspect retains the earlier QA testing interface, and the
existing local ingestion/inspection pages retain their original backend guards.

Guest exchanges remain in the browser tab's session storage with a twelve-hour
expiry and twenty-exchange cap, and are not permanently recorded by /ask.
Signing in retains the transcript; saving or continuing it creates a private
database chat. Guest citation imports verify the current publication, source
span, file hash, page and exact quotation. Saved titles use the first question.
Saved chat context comes from the last ten exchanges in that chat's database
records. Human messages use separate tables, five-second polling, unread counts
and read markers. Lists and message history offer pagination.

The existing retrieval, query understanding, citation verification and provider
budgets remain in use. A narrow Gemini fix preserves the distinct second
fallback when the primary model equals the first fallback. Provider quotas and
outages remain external constraints. OpenAI failover was not live-tested because
no OpenAI credential is configured.

## Database tables

All names below have the platform_ prefix:

| Area | Tables |
| --- | --- |
| Identity/security | accounts, wakeels, sessions, reset_tokens, rate_buckets |
| AI | chats, chat_messages |
| Human messaging | conversations, messages, preparations |
| Agreements | agreements, agreement_versions, agreement_events, acceptances |
| Cases | cases, case_updates, attachments |
| Oversight | reviews, notifications, audit, reports |

The additive migration is alembic/versions/20261126_20_platform.py. It includes
foreign keys, indexes, unique constraints and status checks. Downgrade refuses to
delete private histories; recovery requires an operator-managed backup/restore.

## API map

Every path in this table is prefixed with /platform. OpenAPI contains the exact
input schemas. Mutations require X-LegalMind-Client: web and an allowed Origin
when one is present. List endpoints accept offset and return up to 200 records.

| Area | Methods and paths |
| --- | --- |
| Auth | POST /auth/register, /auth/login, /auth/logout, /auth/refresh, /auth/profile, /auth/password, /auth/reset; GET /auth/me |
| Saved AI | GET/POST /chats; POST /chats/import; GET /chats/{id}; POST /chats/{id}/ask |
| Discovery | GET /wakeels, /wakeels/{id} |
| Human messages | GET/POST /conversations; GET /conversations/{id}; POST /conversations/{id}/messages, /conversations/{id}/read |
| Preparation | GET/POST /preparations |
| Service agreements | GET/POST /agreements; GET /agreements/{id}; POST /agreements/{id}/actions |
| Cases | GET/POST /cases; GET /cases/{id}; POST /cases/{id}/updates, /cases/{id}/attachments; GET /cases/{id}/attachments/{attachment_id} |
| Reviews/notifications | GET/POST /reviews; GET /notifications; POST /notifications/{id}/read |
| Reports/admin | POST /reports; GET /admin, /admin/reports/{id}, /admin/disputes/{id}; POST /admin/actions |

Existing /ask and document ingestion contracts keep their original purpose and
response schemas. Service agreements are separate from ingested legal documents.

## File map

| Backend files | Purpose |
| --- | --- |
| app/platform/models.py, schemas.py | Platform persistence and validated inputs |
| app/platform/security.py, common.py | Sessions, passwords, CSRF, counters, transactions and ownership |
| app/platform/auth.py, chats.py | Authentication and saved AI |
| app/platform/marketplace.py, agreements.py | Discovery, messaging, requests and contract lifecycle |
| app/platform/cases.py, admin.py, router.py | Cases, reviews, notifications, admin and route wiring |
| alembic/env.py and new migration | Model registration and additive schema |
| scripts/manage_accounts.py | Trusted administrator/recovery CLI |
| app/main.py, app/api/middleware.py | Route mounting and bounded attachment bodies |
| app/api/routes/qa.py, app/qa/service.py | Disable permanent guest QA audit recording |
| app/qa/llm.py | Preserve the distinct Gemini fallback |
| tests/test_platform.py, test_platform_security.py, platform_server.py | API/security tests and isolated browser server |
| tests/test_api.py, requirements-dev.txt | Cancellation regression compatibility and SQLite test dependency |

| Frontend files | Purpose |
| --- | --- |
| src/app/page.tsx, layout.tsx, platform.css | Homepage, navigation/session wrapper and responsive styles |
| src/app/[...path]/page.tsx | Validated platform routes |
| src/components/platform/session.tsx, auth-pages.tsx | Session lifecycle and authentication forms |
| src/components/platform/chat.tsx | Guest/saved AI, citations, retry and history |
| src/components/platform/marketplace.tsx | Dashboards, Wakeels, human messages and preparation |
| src/components/platform/agreement-pages.tsx | Drafts, negotiation, immutable versions and signing |
| src/components/platform/management.tsx | Cases, profiles, settings, notifications and admin |
| src/components/platform/ui.tsx, pages.tsx | Reusable controls, error/loading states and pagination |
| src/lib/platform.ts, proxy-body.ts | Typed API client and bounded proxy requests |
| src/app/api/platform/[...path]/route.ts, src/app/api/ask/route.ts | Same-origin proxies, cookies and server-only API key |
| src/app/inspect/page.tsx | Retained QA inspection tool |
| next.config.ts, .env.example, package manifests | Security headers, environment template and patched Next.js |
| Playwright configs and tests | Platform workflows and retained-tool regressions |

## Additional deployment notes

Use npm ci in the frontend and python -m pip install -r requirements.txt in the
backend. Reuse existing ignored environment files. Set frontend BACKEND_URL to
the backend origin; set BACKEND_API_KEY only on the Next.js server if /ask requires
a key. NEXT_PUBLIC_API_BASE_URL remains for the local inspection tools.

Anonymous IP counters see the proxy source in the default local topology.
Configure edge rate limiting and trusted proxy handling for public multi-instance
deployment. Authenticated counters follow accounts across API workers.

Password delivery, email/SMS notifications, payments, external identity/e-signature
services, malware scanning and production hosting are not configured. Messaging
and notifications use polling. Load/penetration testing has not been performed.
Previously exposed provider keys and the database password still require rotation
through their owning accounts; this delivery does not claim credential rotation.
Pull the pushed branches and restart services on any separate/deployed checkout.
