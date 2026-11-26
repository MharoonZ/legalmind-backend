# Website optimization delivery — 26 November 2026

This update preserves the FastAPI QA pipeline, reviewed corpus, account roles, agreements, cases, private files and existing Next.js tools. The backend branch is `feature/ask`; the frontend branch is `shimza`.

## Measured performance

Measurements used the local production API and the existing Sydney Supabase session pooler, with four consecutive requests per endpoint over one HTTP client. These are small development measurements, not a load test or a production latency guarantee.

| Check | Before | After |
| --- | ---: | ---: |
| Anonymous session probe, median | 3,349 ms | 6.15 ms |
| Public Wakeel directory, median | 6,651 ms | 6.7 ms with the new cache |
| First uncached directory request in the after run | — | 4,837.5 ms |
| Subsequent three directory requests | — | 5.3 / 7.0 / 6.4 ms |
| Initial /ask JavaScript, gzip bytes | 222,691 | 184,286 |
| Initial /ask JavaScript, uncompressed bytes | 740,123 | 598,488 |

The initial HTML script payload fell by 17.25% compressed (19.14% uncompressed). This measures script tags in /ask HTML, not all later dynamic downloads.

The slow database path still matters on a cache miss. Reducing round trips and serving unchanged public results locally makes repeat navigation substantially faster; moving the API closer to the database is still useful for initial authenticated reads.

Actual bottlenecks and changes:

- Anonymous session probes previously opened database work and consumed a database throttle transaction. They now return the same 401 response without querying the database.
- Authentication and endpoint queries share one request transaction. Session/account validation uses one joined query. Ordinary user/admin session responses no longer query the Wakeel profile table.
- General platform throttling reserves at most ten attempts against the shared database ceiling. Login, recovery, password and socket-ticket throttles remain strict per-attempt database counters. Separate locks avoid serializing unrelated users.
- Wakeel list ratings, conversation peers/unread counts, and administration counts use consolidated queries. Case detail pages do not fetch attachment binary contents.
- A public-only directory cache holds at most 32 filtered pages for 15 seconds, deduplicates concurrent loads, and invalidates after committed profile, verification, suspension/restoration and review changes. PostgreSQL broadcasts invalidations to other workers. Cache use stops if the listener disconnects; late reads cannot repopulate an invalidated entry.
- Frontend data is deduplicated and retained for 30 seconds in a bounded, user-scoped memory cache. Relevant HTTP mutations and WebSocket events update or invalidate it. Private responses still use HTTP `no-store`; credentials and private data are not put into a shared Next.js cache.
- Focus/session probes are deduplicated. Session changes clear cached private data and reject stale in-flight identity results.
- Feature screens load through dynamic imports. The App Router shell, route validation and metadata remain server-rendered. Source URL validation no longer imports the full Zod response validator into the chat.
- AI history loads 50 turns at a time with a stable timestamp/ID cursor. Existing list and human-message pagination remains available.
- System fonts, CSS/SVG icons and small loading placeholders avoid external font/image downloads. No runtime dependency was added.

## Themes and chat behavior

The navigation theme control applies to every platform page and the retained QA, ingestion and PDF tools. Semantic colors cover surfaces, text, borders, buttons, inputs, notices and source details. Preference persists in local storage and synchronizes between tabs. A small initial script applies the preference before paint, with the OS preference used initially.

The AI conversation has its own bounded scroll area. The composer stays below it, independent of message length. Sending clears the composer and immediately displays the user message. New messages scroll into view when the reader is near the bottom; reading older messages preserves position, with an accessible latest-message control. Loading older history preserves the visible scroll anchor. Reduced-motion and IME input are respected.

## Gemini conversation titles

Saved conversations enqueue a durable title job after the first exchange, including imported guest conversations. Existing saved conversations are also eligible after migration. The existing Gemini integration generates a short contextual title and persists it; it does not copy the first question.

Title generation runs after the answer transaction, with an eight-second model budget, a small output allowance and no OpenAI failover. A failure cannot discard or delay the saved answer. At most three automatic attempts are made, separated by at least one minute. Failed jobs retain a generic title and expose a manual retry. Claims are locked across workers; stale claims are recovered after a worker interruption.

The existing live database contained three successfully persisted Gemini titles at verification time, zero failed title jobs, and zero ready titles equal to their first question. The live server logged a successful title call through the configured Gemini integration. No synthetic accounts were inserted into the live database for these checks.

## Realtime behavior and security

One authenticated WebSocket per browser tab delivers human messages, read receipts, notifications, relevant resource invalidations and completed AI titles. Message sends remain idempotent HTTP mutations; receipt is pushed immediately, with duplicate IDs suppressed. Idle message and notification polling has been removed.

- The browser exchanges its HttpOnly session cookie for a hashed, single-use, 30-second database ticket through a CSRF-protected POST.
- The ticket travels in the first socket frame, never in a URL. WebSocket origins must exactly match backend CORS origins.
- Authentication and ticket redemption validate the current active account/session. Expiration, logout, password changes and suspension terminate the affected connections.
- Fan-out uses transactional PostgreSQL LISTEN/NOTIFY. Rollbacks cannot deliver a successful-looking message. Notification payloads contain IDs and invalidation paths; message bodies and generated titles are hydrated privately before delivery.
- Recipient-only delivery, bounded queues/connections/frame sizes, heartbeat timeouts, retry backoff, online/offline handling and cleanup protect the connection lifecycle.
- Reconnection refreshes active HTTP snapshots to recover missed events. Unread counts come from the server, avoiding double increments when an HTTP snapshot races a socket event.
- The connection stays shared during client navigation because notifications and titles remain useful across pages. It closes on logout, provider unmount or tab exit.

### Deployment configuration

Apply `python -m alembic upgrade head` before starting the new backend.

`20261126_21` adds three title-job fields/index to `platform_chats` and the RLS-enabled `platform_socket_tickets` table. It preserves corpus data and existing accounts/messages. It was tested in an isolated schema on live Supabase with upgrade/downgrade/upgrade, then rolled back before the real additive upgrade. The live revision is now `20261126_21`.

The listener needs a direct PostgreSQL connection or a **session** pooler. The current Supabase port 5432 connection was verified. Do not use transaction pooling for LISTEN. Each API worker consumes one additional dedicated listener connection.

For local frontend development:

```dotenv
BACKEND_URL=http://127.0.0.1:8000
NEXT_PUBLIC_WEBSOCKET_URL=ws://127.0.0.1:8000/platform/ws
```

For deployment, set `NEXT_PUBLIC_WEBSOCKET_URL=wss://YOUR_API_HOST/platform/ws` before building, allow the exact frontend origin in backend `CORS_ORIGINS`, and configure the proxy to support WebSocket upgrades and long-lived connections. If the public WebSocket URL is omitted on HTTPS, the frontend uses `wss://<frontend-host>/platform/ws`; that path must be reverse-proxied to FastAPI. The Next.js HTTP route handler is not a WebSocket proxy.

Keep database/provider secrets in ignored backend environment files. This update does not rotate previously exposed credentials.

## Passwords and dependency security

Registration, password changes, password reset schemas, frontend forms and the administrator CLI consistently accept a minimum of six characters. Existing hashing, maximum lengths, throttles, authorization and session revocation remain.

The requested `npx next upgrade --agent=security` completed and reported that **Next.js 16.4.6 needs no security update**. This checkout already had the patched version. React/React DOM remain 19.3.1. No package or lockfile change was needed for this update. A fresh npm audit reported zero vulnerabilities, including development dependencies.

## Validation

- Backend: **749 passed, 5 skipped** in the final full suite. Ruff passed. The skipped tests require optional external integration configuration.
- Five platform browser scenarios passed: guest-to-saved chat and login/logout/back navigation; instant messaging, offline recovery, no polling, preparation/revisions/signing/completion/reviews/cases; six-character registration/admin access and responsive pages; theme persistence, cached navigation and 55-turn chat pagination/scrolling; retained tools in both themes at 1440, 768 and 390 pixels.
- The chat regression checks verify input clearing, visible pending user text, generated title persistence, no document-level overflow, bottom composer placement and scroll recovery after loading earlier turns.
- Realtime/backend regressions cover origin rejection, ticket replay/expiry, unauthorized recipients, logout revocation, idempotency, server unread counts, shared quota ceilings, title failure/retry/crash recovery and cache invalidation during in-flight reads.
- Next.js security upgrade check and npm audit passed. TypeScript, ESLint and the production build passed.
- All **23 retained-tool browser tests** passed against the development server, covering PDF registration, extraction inspection, citation display, cancellation, input limits and narrow screens. Production correctly returns 404 for unauthenticated ingestion/inspection routes.
- Screenshots were reviewed for the homepage, AI conversation, case forms and retained tools. The legacy upload button also passes a 4.5:1 minimum text contrast assertion in both themes.

The isolated browser server uses temporary SQLite accounts and scripted QA/title responses. Its ticket limit is raised only for the responsive matrix's repeated full navigations; production authentication, tickets and throttles are tested separately. No browser-test account or document was written to Supabase.

Direct live checks confirmed database connectivity/readiness, revision/RLS, persisted Gemini titles, cross-worker notification delivery after commit, and no event delivery before commit or after rollback.

## File map

| Backend files | Change |
| --- | --- |
| `app/main.py` | Start/stop realtime and durable title workers |
| `app/platform/realtime.py`, `events.py` | Authenticated sockets and transactional cross-worker events |
| `app/platform/titles.py` | Gemini title queue, claim/retry and validation |
| `app/platform/directory_cache.py` | Bounded public directory cache |
| `app/platform/security.py`, `router.py` | Shared request session, quota leases and revocation |
| `app/platform/auth.py`, `admin.py`, `marketplace.py` | Leaner queries, invalidations and realtime mutations |
| `app/platform/chats.py`, `cases.py` | Cursor history, title trigger/retry and deferred file bodies |
| `app/platform/models.py`, `schemas.py` | Ticket/title fields and six-character validation |
| `app/qa/llm.py`, `scripts/manage_accounts.py` | Optional failover suppression and password minimum |
| `alembic/versions/20261126_21_realtime_titles.py` | Additive migration |
| `tests/test_platform_realtime.py`, `test_platform.py`, `platform_server.py` | Security, concurrency, cache, title and browser regressions |

| Frontend files | Change |
| --- | --- |
| `src/app/layout.tsx`, `theme.css`, `globals.css`, `platform.css` | Theme initialization, colors and responsive layout |
| `src/components/platform/theme.tsx`, `session.tsx`, `realtime.tsx` | Theme/session/socket lifecycle |
| `src/lib/platform-cache.ts`, `platform.ts`, `source-url.ts`, `ask.ts` | Scoped cache, events/types and smaller imports |
| `src/components/platform/chat.tsx`, `marketplace.tsx` | Chat scrolling, title display and instant messages |
| `src/components/platform/ui.tsx`, `pages.tsx` | Deduplicated data hook and lazy feature screens |
| `src/components/platform/auth-pages.tsx`, `management.tsx` | Six-character password forms |
| `src/app/[...path]/page.tsx`, `loading.tsx`, `error.tsx` | Metadata and route-level fallback/retry |
| QA tester, ingestion form and PDF inspector | Global theme controls |
| `.env.example`, `playwright.platform.config.ts`, `tests/platform.spec.ts` | WebSocket configuration and browser checks |

## Remaining operational limits

Uncached remote database reads and model generation still have network/provider latency. Gemini availability and quota can delay titles; the bounded generic-title fallback remains deliberate. This delivery does not establish load-test or penetration-test coverage.

Production hosting, email verification/delivery, recovery email delivery, malware scanning, external identity/signature providers and credential rotation remain separate operational work. The earlier platform documentation describes these limits in more detail.

References: [Next.js lazy loading](https://nextjs.org/docs/app/guides/lazy-loading), [FastAPI WebSockets](https://fastapi.tiangolo.com/advanced/websockets/), [PostgreSQL NOTIFY transaction behavior](https://www.postgresql.org/docs/current/sql-notify.html).

Implementation commits: backend [1089f9d](https://github.com/MharoonZ/legalmind-backend/commit/1089f9d) and frontend [3d83c33](https://github.com/MharoonZ/legalmind-frontend/commit/3d83c33).
