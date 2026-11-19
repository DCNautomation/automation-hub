# QA Report: Automation Hub dashboard and API (2026-10-30)

**URL:** http://127.0.0.1:8080 (dashboard), http://127.0.0.1:8000 (API), plus the public quick tunnel address for the exposure check
**Scope:** login page, runs board, run detail, 404 pages, sign out, signed ingest, the two changes made today (error line in each row, 5 second refresh), and the full API and UI test suites
**Tested at:** desktop 1440, tablet 768, mobile 375 (Edge, headless, Playwright)
**Could not test:** Firefox and Safari (Edge only), screen readers, Lighthouse scores, load behaviour, admin retry in the browser (only a viewer login was used), a real host (nothing is deployed)

## Summary
- Critical: 0 | Major: 1 | Minor: 2 (both fixed after this report, see Follow-up) | 1 finding retracted
- The dashboard works. Everything I exercised passed except for the issues below. New runs appeared on the board by themselves in about 2.4 seconds without a reload, and signed ingest, duplicate handling and bad signatures all behaved correctly.
- The one thing to fix before sharing the tunnel address with anyone is that the API's interactive docs are open to the public.
- Top 3 fixes:
  1. Turn off `/docs`, `/redoc` and `/openapi.json` when `HUB_ENV=production`.
  2. Let the error line wrap to two lines so the "To do" part is readable, especially on mobile.
  3. Add `noindex` to the dashboard.

## Issues

### [Major] API docs are public, including through the tunnel
- **Where:** API, `/docs`, `/redoc`, `/openapi.json`
- **What happens:** All three return 200 with no login, locally and through the public tunnel. The schema lists all 11 routes (`/auth/login`, `/ingest/{source}`, `/runs/{job_id}/retry` and so on). Every protected route correctly answers 401 without a token, so nothing can be read or changed. This is an information leak only.
- **Steps to reproduce:** 1. Open `https://ann-neck-anytime-carrying.trycloudflare.com/docs` in any browser.
- **Expected:** In production the docs are off, or behind a login.
- **Fix:** In `api/app/main.py`, create the app with `docs_url=None, redoc_url=None, openapi_url=None` when `HUB_ENV=production`. Add a preflight warning if they are on in production. Keep them on locally.
- **Evidence:** curl results above. `/healthz/worker` is also public and shows worker count and queue lag. Low risk, but the same decision applies.

### [Minor, FIXED] The "To do" part of the error line is often hidden
- **Where:** runs board, `.c-source .err`
- **What happens:** The line is one line, cut at 62 characters on desktop and about 35 on mobile. The app and code are visible ("Stripe 401: Invalid API key provided. T..."), but the instruction is usually cut off. The full text is only in the hover title, which touch screens don't have.
- **Expected:** The reader sees what to do without opening the run.
- **Fix:** Replace the single-line ellipsis with a two line clamp (`display: -webkit-box; -webkit-line-clamp: 2; white-space: normal`).
- **Evidence:** `qa-screenshots-automation-hub/board-mobile.png`, `board-desktop.png`

### [Minor, FIXED] No noindex on the dashboard
- **Where:** `web/index.html`, nginx headers
- **What happens:** No robots meta tag and no `X-Robots-Tag` header on the dashboard. The API already sends `X-Robots-Tag: none` (seen through the tunnel). It is behind a login, so this only matters if the dashboard is ever made public.
- **Fix:** Add `<meta name="robots" content="noindex">` and `add_header X-Robots-Tag "noindex" always;` in the nginx config.

### [Retracted] Sign out request is reported as aborted by the browser
- **Where:** Sign out button, `POST /auth/logout`
- **What happens:** The browser logs `net::ERR_ABORTED` for the logout call. The server still ends the session: the same token returns 401 straight after, from both the UI and a direct API call. So the user is safely signed out, but the client does not wait for the answer.
- **Fix:** Send the logout with `keepalive: true`, or wait for it before clearing local state. Cause not fully confirmed (unverified).
- **Evidence:** script `qa2.mjs` output: "me before 200 | logout requests sent, failed ERR_ABORTED | me after 401".

## Follow-up (same day)
- **Error line:** desktop now wraps to 2 lines (none clipped at the current messages). Mobile shows the whole message, up to 4 lines, with no clipping and no sideways scroll. Checked in Edge.
- **noindex:** added `<meta name="robots" content="noindex, nofollow">` in `web/index.html` and `X-Robots-Tag: noindex, nofollow` in `web/nginx.conf.template`. Both confirmed on the rebuilt dashboard.
- **Sign out "abort" retracted:** a plain `fetch` to `/auth/logout` from inside the page, outside the app code, returned a normal 204 with `ok: true`, and the same request still showed `ERR_ABORTED` in the test tool. It is how headless Edge reports a 204 with no body, not an app bug, and the server really ends the session. I tried adding `keepalive` first, saw no change, and reverted it with its test.
- **Still open:** the Major finding (public API docs). Not touched.
- UI tests after the follow-up: 38 passing.

## Not a bug, noted so nobody chases it
- Opening `/runs/999999` logs one 404 in the browser console. That is the app asking for a run that does not exist and then showing "not found". No other console errors or page errors appeared in the whole run.

## What passed
- Login page: lang set, one h1, labelled inputs, Sign in disabled until both fields are filled, visible focus ring on first Tab, loads in about 0.75 s, no horizontal scroll at 1440, 768 and 375.
- Runs board at all three widths: no horizontal scroll, no control under 24 px, error lines stay inside the screen on mobile.
- Error line: shows on all 6 failed rows, hides the "RuntimeError:" prefix, hover title holds the full text. Colour contrast is about 8.4 to 1 (calculated from the colours, not measured by a tool).
- Auto refresh: the board polled `/runs` every 4,996 ms. A new signed event appeared with no reload in 2.4 s.
- Ingest: valid signature 202, same event again 200 (duplicate), wrong signature 401.
- Run detail: payload, tries and history shown, one h1, no retry offered to a viewer.
- 404 page ("No such page" with a Back to runs button) and unknown run id both work.
- Sign out returns to login and the old token is refused by the API.
- Security headers on the dashboard: strict CSP, nosniff, no-referrer, frame-ancestors none, permissions policy.
- Every protected API route answers 401 without a token, including through the public tunnel.

## Test suites (senior QA)
- **API:** 374 passed, 97% line and branch coverage (1,239 statements, 29 missed). Run on a separate `hub_test` database, so live data was not touched.
- **UI:** 38 passed. I added 2 tests today because the new features had none: one for the error line (shown with full text in the title, absent when there is no error) and one for the 5 second refresh (no call at 4.9 s, a call after 5 s).
- **Mutation check on the new tests:** I broke each feature on purpose (refresh back to 10 s, error line removed). Each change made its test fail, and I restored the file after.
- **Weakest covered files:** `maintenance.py` 77% (lines 33 to 37, the cleanup delete paths), `users.py` 88%, `dbroles.py` 89%, `worker.py` 89% (error paths around lines 51 to 58 and 82 to 89). The cleanup job is the one worth covering first, since it deletes data.
- **Note on the skill:** the `senior-qa` scripts are stubs that print success without analysing anything, so I did not use them. Everything above came from running the real suites and a real browser.

## Recommended fix order
1. Disable API docs in production and add a preflight warning (before the tunnel address is shared).
2. (Done) Two line wrap for the error text.
3. (Done) noindex meta and header.
4. Add tests for `maintenance.py` cleanup.

## Cleanup done after testing
- Deleted the temporary `qa-viewer` login and the one QA test run (the `QA-check` event). The audit log keeps its rows because it is append-only.
