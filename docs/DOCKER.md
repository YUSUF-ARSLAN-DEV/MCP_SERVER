# Running it in Docker

One container holds the web app and the pipeline it launches for each run (with Chromium). Everything that must survive a
restart lives in one volume, `/data`: the database (`app.db`), and a scratch folder per run.

## Start it

```bash
docker compose up --build
```

That is all. Add `-d` to run it in the background. It needs no other setup.

To change settings, copy `docker.env.example` to `docker.env` and edit it (optional). Before sharing the app with anyone,
set your own `SESSION_SECRET` / `SECRETS_KEY` there.

Open <http://127.0.0.1:8000>. You get a sign-in / **Create account** page. There are no administrators: everyone is just a
user and sees only their own runs and saved models. `MAX_SIGNUPS_PER_DAY=0` closes sign-ups.

## Start again from empty

To delete every account, run, saved model and stored file but keep the tables:

```bash
docker compose exec app python3 -m api.reset --yes
```

(Without `--yes` it only shows what it would remove.)

Each person then saves their own model on the **Models** page (provider, model name, API key). There is no model key in
the container's settings; saved keys are stored encrypted in the database.

## What lives where

| Thing | Where | Notes |
|---|---|---|
| Accounts, runs, run summaries, **report and evidence files**, saved models | `/data/app.db` | the one file to back up |
| Key that encrypts saved API keys | `SECRETS_KEY` in `docker.env`, else `/data/secrets.key` | set `SECRETS_KEY` to keep it apart from the database |
| A run's working files | `/data/runs/<run>/` | scratch: moved into the database and deleted when the run ends |
| Settings | `docker.env` (optional) | never baked into the image |

## Back it up

The database is the whole state. Take a consistent copy while the app is running:

```bash
docker compose exec app python3 -c "import sqlite3; s = sqlite3.connect('/data/app.db'); d = sqlite3.connect('/data/backup.db'); s.backup(d); d.close()"
docker compose cp app:/data/backup.db ./app-backup.db
```

Keep `SECRETS_KEY` (or `secrets.key`) with it: without the key, saved API keys cannot be read and people must enter them again.

Restore: stop the app, copy the backup over `app.db` in the volume, start it.

## Update

```bash
git pull
docker compose up -d --build
```

The volume is untouched; the database upgrades itself on start.

## Sharing it with other people

By default the app listens on this machine only (`127.0.0.1:8000`). To share it, put a reverse proxy that does **HTTPS** in
front (Caddy, nginx, Traefik) and then set `COOKIE_SECURE=1` and `TRUST_PROXY=1` in `docker.env` and recreate the
container. Do not expose port 8000 to the internet directly: sign-in cookies and API keys would travel unencrypted.

Size the server for the runs: each concurrent run is a real browser (about 1.5 GB). `MAX_CONCURRENT_JOBS` caps how many
run at once; the rest wait in a queue.

## Limits worth knowing

- Chromium only, and a run cannot sign in to a site unattended (a person answers CAPTCHAs and sign-ins on the page).
- Files over `MAX_STORED_FILE_MB`, or past `MAX_STORED_RUN_MB` for a run, are left out of the database and listed on the
  run's page as left out; the report is always kept first.
- The database grows with every run. There is no automatic clean-up yet.
- Testing a site on a private network needs `ALLOW_PRIVATE_TARGETS=1`, which on a shared server would let any user make
  the server browse its own network. Leave it off unless the server is yours alone.
