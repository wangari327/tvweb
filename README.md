# iBOX TV

iBOX TV is a Flask catalogue for TV shows, anime, and movies. It ingests
availability updates from Telegram, enriches titles with TMDB metadata, and
routes visitors to the available source. The public site has one canonical
domain, category landing pages, detail pages, XML sitemaps, a Redis-backed
popularity leaderboard, and AdSense-ready page layouts.

The live project is intended to run at `https://ibox-tv.com`. Historic hosts
redirect to the canonical paths configured by `SITE_BASE_URL` and
`LEGACY_SITE_HOSTS`.

## What runs in production

| Process | Purpose |
| --- | --- |
| Flask + Gunicorn | Public catalogue, search, sitemaps, detail pages, redirects |
| Celery worker | Telegram ingestion, metadata enrichment, movie backfills |
| Celery beat | Scheduled update and leaderboard-reset jobs |
| PostgreSQL | Catalogue, metadata, genres, and ingestion state |
| Redis | Celery broker/results, application cache, and rolling popularity leaderboard |
| Nginx (VPS only) | TLS termination, static files, and reverse proxying |

PostgreSQL and Redis are required in every production environment. MongoDB is
only required when using the optional movie-backfill source.

## Repository layout

| Path | Use |
| --- | --- |
| `tv_app/` | Flask application, templates, assets, models, and Celery tasks |
| `scripts/initialize_database.py` | Creates the current schema in a new database |
| `scripts/migrate_seo_enrichment.py` | Targeted migration for an older, existing database |
| `scripts/backfill_seo_metadata.py` | Enriches existing titles with TMDB metadata |
| `scripts/build_catalog_indexes.py` | Builds catalogue indexes without blocking public traffic |
| `deploy/` | Reproducible Ubuntu bootstrap, release, systemd, and Nginx tooling |
| `Procfile` | Web, worker, and scheduler process definitions for PaaS platforms |

## Configuration

Copy `.env.example` to `.env` for local development. On a VPS the deploy
scripts keep the real file at `/opt/ibox-tv/shared/.env` and copy it into each
immutable release with restrictive permissions.

At a minimum, set the following real values:

```ini
SECRET_KEY=<long random secret>
ADMIN_TOKEN=<different long random secret>
SITE_BASE_URL=https://your-domain.example
DATABASE_URL=postgresql://user:password@host:5432/database
REDIS_URL=redis://host:6379/0
TELEGRAM_BOT_TOKEN=<telegram bot token>
TELEGRAM_CHANNEL_ID=-100...
TELEGRAM_ANIME_CHANNEL_ID=-100...
TMDB_BEARER_TOKEN=<tmdb bearer token>
```

`TMDB_BACKFILL_TOKENS`, `MONGO_URI_1`, `MONGO_URI_2`, `MONGO_DB_NAME`, and
`MONGO_COL_NAME` are optional unless the movie backfill engine is enabled.
Keep all production secrets out of Git. The repository ignores `.env` while
retaining the safe `.env.example` template.

## Local development

Use Python 3.10 or newer, PostgreSQL, and Redis.

```bash
git clone https://github.com/wangari327/tvweb.git
cd tvweb
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt
cp .env.example .env
# Edit .env before continuing.
python scripts/initialize_database.py
flask --app tv_app.app run
```

Run the asynchronous processes in separate terminals after the web server is
up:

```bash
.venv/bin/celery -A tv_app.tasks worker --loglevel=INFO --concurrency=1
.venv/bin/celery -A tv_app.tasks beat --loglevel=INFO
```

For an existing database, do **not** use an old hand-written `CREATE TABLE`
script. Run a purpose-built migration from `scripts/` when a release calls for
one, then deploy the application. `scripts/initialize_database.py` is
idempotent and is the correct command for a brand-new database.

## Fresh Ubuntu VPS deployment

The `deploy/` scripts support Ubuntu 22.04 and 24.04 and use systemd rather
than Supervisor. They install OS prerequisites, create an unprivileged service
account, build a shared virtual environment, make immutable releases, and
serve the application through Nginx. The default layout is:

```text
/opt/ibox-tv/
  repository/       deployment checkout
  releases/<commit>/ immutable application releases
  current -> releases/<commit>
  shared/.env       only copy of the secrets outside active releases
  shared/venv/      Python virtual environment
```

### 1. Prepare DNS and database services

Point the domain's `A` record to the VPS. Decide whether PostgreSQL will be
local or managed; managed PostgreSQL is usually the easier recovery and backup
choice. Redis may also be managed, though the bootstrap installs a local Redis
server by default.

To host PostgreSQL on the VPS, include `INSTALL_POSTGRES=1` in the bootstrap
command and create a least-privilege database user before the first release:

```bash
sudo -u postgres createuser --pwprompt ibox
sudo -u postgres createdb --owner=ibox ibox_tv
```

Set the matching `DATABASE_URL` in the environment file. The PostgreSQL user
must be allowed to create the `pg_trgm` extension, or an administrator must
run `CREATE EXTENSION IF NOT EXISTS pg_trgm;` once on the database.

### 2. Bootstrap the server

Log in as root (or a sudo-capable user), clone this repository somewhere
temporary, then run the bootstrap. It deliberately does not start the app
until secrets are configured.

```bash
git clone https://github.com/wangari327/tvweb.git /tmp/tvweb-bootstrap
cd /tmp/tvweb-bootstrap
REPOSITORY_URL=https://github.com/wangari327/tvweb.git \
  INSTALL_POSTGRES=1 \
  bash deploy/bootstrap_ubuntu.sh
```

Omit `INSTALL_POSTGRES=1` when `DATABASE_URL` points to a managed provider.
Then edit the configuration:

```bash
nano /opt/ibox-tv/shared/.env
```

### 3. Create the first release and start services

```bash
APP_ROOT=/opt/ibox-tv APP_USER=ibox \
  bash /opt/ibox-tv/repository/deploy/deploy_release.sh

APP_ROOT=/opt/ibox-tv APP_USER=ibox \
  bash /opt/ibox-tv/repository/deploy/install_systemd.sh
```

The release command installs Python dependencies, creates missing database
objects, switches the `current` symlink atomically, and checks Gunicorn on
`127.0.0.1:8000` when services already exist. It never calls `FLUSHALL` on
Redis: Celery uses that Redis instance as its broker.

### 4. Add Nginx and TLS

```bash
DOMAIN=your-domain.example APP_ROOT=/opt/ibox-tv \
  bash /opt/ibox-tv/repository/deploy/install_nginx.sh

certbot --nginx -d your-domain.example -d www.your-domain.example
```

If the hostname is proxied through Cloudflare, use a DNS challenge or
temporarily switch the record to **DNS only** while Certbot performs HTTP
validation. After the certificate is issued, re-enable the proxy if desired.

The supplied Nginx configuration caches static assets only. It intentionally
does not cache dynamic catalogue pages, avoiding stale pages immediately after
a deployment; Redis already supplies the application-level cache.

## Deploying an update on the VPS

After changes are pushed to `main`, update the deployment checkout and run the
release script as root:

```bash
git -C /opt/ibox-tv/repository pull --ff-only origin main
APP_ROOT=/opt/ibox-tv APP_USER=ibox \
  bash /opt/ibox-tv/repository/deploy/deploy_release.sh
```

Useful operational checks:

```bash
systemctl status ibox-tv-web ibox-tv-worker ibox-tv-beat
journalctl -u ibox-tv-web -u ibox-tv-worker -u ibox-tv-beat -f
curl -fsS http://127.0.0.1:8000/robots.txt
```

To roll back, point `current` to a known-good release and restart the three
services. Keep several confirmed releases until the new version is stable:

```bash
ln -sfn /opt/ibox-tv/releases/<known-good-commit> /opt/ibox-tv/current
systemctl restart ibox-tv-web ibox-tv-worker ibox-tv-beat
```

An older iBOX TV Nginx setup may have a dynamic `proxy_cache`. When migrating
that setup, stop Nginx, clear only its configured iBOX cache directory, and
restart it after changing a release. Do not clear the entire Redis database.

## Railway, Heroku, and similar PaaS platforms

Use managed PostgreSQL and Redis, then deploy **three processes from this same
repository**. The checked-in `Procfile` provides the commands:

| PaaS service | Process type / start command | Instances |
| --- | --- | --- |
| Public web service | `web` | 1 or more |
| Background worker | `worker` | exactly 1 on a small catalogue |
| Scheduler | `beat` | exactly 1 |

Set every value from the Configuration section as the platform's environment
variables. Set `SITE_BASE_URL` to the final HTTPS custom domain, attach that
domain, and run `python scripts/initialize_database.py` once in a release shell
or one-off job before starting traffic. Never run more than one `beat` process,
or scheduled Telegram and maintenance jobs will run twice.

On Railway, create three services from the same repository and assign the web,
worker, and beat commands above; attach its PostgreSQL and Redis services. On
Heroku or a comparable Procfile platform, provision equivalent Postgres/Redis
add-ons and scale one each of `web`, `worker`, and `beat`. PaaS platforms do
not need the Ubuntu `deploy/` directory or Nginx.

## Search, SEO, ads, and cache notes

- Canonical URLs and permanent legacy-host redirects are generated from
  `SITE_BASE_URL` and `LEGACY_SITE_HOSTS`.
- `/sitemap.xml` is a sitemap index with category sitemaps. Submit that URL in
  Google Search Console; keep search/filter pages crawlable but `noindex`.
- Detail pages are included only when a title has a valid availability link,
  poster, overview, name, and slug. The metadata-enrichment scripts improve
  page quality for indexing.
- `ads.txt` is intentionally retained for AdSense. Ad slots are page-layout
  code, not part of the deployment cache.
- Redis leaderboard and cache keys expire automatically. Configure Redis with
  a memory limit and `noeviction` when it doubles as Celery's broker, so memory
  pressure does not discard queued tasks.

## Validation before publishing a change

```bash
pytest -q
python -m py_compile tv_app/*.py scripts/*.py
```

For a release that changes templates or styles, also check the homepage, all
three category landing pages, browse/search, a detail page, `robots.txt`,
`sitemap.xml`, and mobile navigation after deploy.

## Security and backups

- Use long, unique values for `SECRET_KEY` and `ADMIN_TOKEN`.
- Keep PostgreSQL backups and test a restore before relying on them.
- Restrict port 8000 to localhost; only Nginx should expose HTTP/HTTPS.
- Restrict PostgreSQL and Redis to private networking or localhost.
- Rotate Telegram, TMDB, database, and Redis credentials if they are ever
  exposed in a terminal, screenshot, chat, or commit.
