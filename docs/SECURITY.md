# Security notes

What the server protects, what you must do when you deploy it, and what it does not do. Reviewed and hardened on
2026-10-24; every protection below has a test (`tests_python/test_api_*.py`, `test_netguard.py`, `test_documents.py`).

## What is protected

| Risk | Protection |
| --- | --- |
| Someone reads another person's runs, reports or evidence | every run belongs to one account; someone else's run is a 404, not a 403, so it is not even confirmed to exist |
| Password guessing | passwords are salted scrypt hashes; wrong sign-ins are limited per address and per username; changing a password ends every session |
| Sign-ups used to flood the server | per-address and per-day limits (`SIGNUPS_PER_ADDRESS_PER_HOUR`, `MAX_SIGNUPS_PER_DAY`, 0 closes sign-ups) |
| A stolen model API key | saved keys are encrypted (Fernet) with `SECRETS_KEY`, can never be read back through the API, and a run's copy is deleted when the run ends |
| "Test this site" used to probe the server's own network | the typed address must resolve to a public address; **and** every request the run's browser makes (redirects, scripts, images) is refused if it resolves to a private one (`netguard.py`); the run's own link checks obey the same rule |
| A page framed or scripted from another site | `Content-Security-Policy` (default-src 'self', no framing), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer` |
| Cross-site request forgery | the session cookie is `HttpOnly` and `SameSite=Lax`; a write whose `Origin` is another site is refused (`Cross-site request refused`) before any route runs |
| Session cookie over plain http | the cookie is `Secure` automatically whenever the page came over https (or a trusted proxy says so); `Strict-Transport-Security` is sent over https |
| Cached private data | every `/api/` response is `Cache-Control: no-store` |
| Huge uploads | request bodies over ~48 MB are refused; a requirements document is limited to 8 MB, a `.docx` that unpacks to more than 60 MB or a PDF over 300 pages is refused before it is parsed |
| A test run creating real records on the site under test | the explorer never presses the button of a form that asks for personal details (e-mail, phone, password, card ...), not even empty, unless `ALLOW_FORM_SUBMIT=1`; the sweep types into fields but never submits |
| One run starving the machine | at most `MAX_CONCURRENT_JOBS` at once, one active run per person, `MAX_JOBS_PER_DAY` per person, `JOB_TIMEOUT_S` per run, `MAX_PAGES_LIMIT` pages |
| Server credentials reaching a run | a run's process does not inherit the server's `.env` (see `api/jobs.py: run_environment`) |
| The container | runs as a non-root user; the image is built from an allow-list (`.dockerignore`), so `.env`, saved logins and `runs/` can never be in it |

## What you must do when you deploy

1. **Serve it over https** (a reverse proxy such as Caddy or nginx) and set `TRUST_PROXY=1` so the real client address and
   the `X-Forwarded-Proto` header are used. Set `COOKIE_SECURE=1` to be explicit.
2. If the proxy rewrites the `Host` header, list the public origin in `ALLOWED_ORIGINS=https://your.host` or writes from
   the page will be refused as cross-site.
3. **Set `SECRETS_KEY` and `SESSION_SECRET` from your host's secret store.** Left unset, they are generated into the
   data volume next to the database, so anyone who copies the volume gets both.
4. Leave `ALLOW_PRIVATE_TARGETS` and `ALLOW_PRIVATE_MODEL_URLS` off on a shared server.
5. Back up the data volume; the database holds accounts, saved (encrypted) keys and every stored run.
6. Add **network-level egress rules** for the container (block the metadata address 169.254.169.254 and your private ranges).
   The browser guard resolves names itself, so a name that changes its answer between the check and the browser's own
   lookup (DNS rebinding) is only stopped by the network.

## What it does not do

- No two-factor sign-in, no password reset by email, no account lockout beyond the rate limits.
- Runs click real buttons on the target site: only test sites you are allowed to test.
- A run executes AI-written test code in a Chromium that has the server's network; the egress rules above are the real fence.
- It has not been tested by an independent third party.
