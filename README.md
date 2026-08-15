# 📺 iBOX TV (Hybrid Platform) — The Complete Guide

> **Version:** Hybrid v2.0 — TV + Anime + Movies with Infinite Backfill Engine

This guide explains how to deploy **iBOX TV**, a production-grade streaming platform that serves **three content verticals from a single codebase**. It also documents the **Infinite Backfill Engine**, which continuously imports movies from external MongoDB sources while respecting API rate limits.

---

## 🏗️ 1. How It Works (Architecture Overview)

### 1.1 Canonical URL Architecture

The public catalogue now uses one canonical domain. Historic subdomains permanently redirect to category paths on that domain.

| Canonical path | Content served | Database filter |
|------|---------------|----------------|
| **ibox-tv.com/** and **/browse/tv** | TV shows | `category = 'tv'` |
| **ibox-tv.com/anime** and **/browse/anime** | Anime | `category = 'anime'` |
| **ibox-tv.com/movies** | Movies | `category = 'movie'` |

Detail URLs use stable TMDB-ID and title paths, such as `/tv/123-the-ark`. Legacy `/show/<slug>` URLs and wrong-category paths return permanent redirects to the canonical detail URL.

The root `/sitemap.xml` is a sitemap index. It links to category-specific sitemap files capped at 25,000 complete, available records each. Search and filter URLs remain crawlable but use `noindex,follow`; they must not be blocked in `robots.txt`.

### 1.2 Backend Components

- **Flask + Gunicorn** — Handles all HTTP traffic
- **Celery + Redis** — Background workers (ingest, backfill, scheduled jobs)
- **PostgreSQL** — Primary application database
- **MongoDB (External)** — Read-only movie source for backfilling

---

## 🧰 2. Server Prerequisites

### Operating System
- Ubuntu **20.04 / 22.04 LTS** (clean VPS recommended)

### 2.1 Install Required System Packages

Run as **root**:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y \
  python3.8 python3.8-venv python3.8-dev \
  build-essential git libpq-dev \
  nginx redis-server supervisor \
  certbot python3-certbot-nginx
```

---

## 📦 3. Application Installation

Clone the repository and prepare the Python environment.

```bash
cd /root
git clone https://github.com/<your-username>/tvweb.git
cd /root/tvweb

# Create isolated virtual environment
python3.8 -m venv venv

# Upgrade build tools
./venv/bin/pip install -U pip wheel

# Install dependencies
./venv/bin/pip install -r requirements.txt
```

---

## 🔐 4. Configuration (.env File)

Create the environment file:

```bash
nano /root/tvweb/.env
```

Fill **every value carefully**.

### 🌐 Core Web & Security

```ini
SECRET_KEY=change_this_to_something_very_long_and_random
FLASK_ENV=production

# Primary canonical domain (SEO + absolute URLs)
SITE_BASE_URL=https://ibox-tv.com

# Historic hosts that should permanently redirect to SITE_BASE_URL
LEGACY_SITE_HOSTS=anime.ibox-tv.com,movies.ibox-tv.com,www.ibox-tv.com

# Admin panel master password
ADMIN_TOKEN=YourSuperSecretPassword
NUKE_COOKIE_TTL_DAYS=30
```

### 🗄️ Database & Cache

```ini
DATABASE_URL=postgresql://myuser:mypassword@localhost/tv_shows_db
REDIS_URL=redis://localhost:6379/0
```

### 🎬 Movie Backfill Engine (MongoDB Sources)

The backfill engine scans **multiple MongoDB clusters sequentially**.

```ini
MONGO_URI_1=mongodb+srv://user:pass@cluster1.mongodb.net/?retryWrites=true&w=majority
MONGO_URI_2=mongodb+srv://user:pass@cluster2.mongodb.net/?retryWrites=true&w=majority

MONGO_DB_NAME=Huswy
MONGO_COL_NAME=Husw
```

### 🍿 TMDb & Telegram Integration

```ini
# Rotated automatically to avoid 429 rate limits
TMDB_BACKFILL_TOKENS=eyJhbGciOi...,eyJhbGciOi...

# Default token for user searches
TMDB_BEARER_TOKEN=eyJhbGciOi...

# Telegram bot username (no @)
BOT_USERNAME=iBoxTVBot

TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHANNEL_ID=-1001234567890        # TV
TELEGRAM_ANIME_CHANNEL_ID=-1009876543210  # Anime
```

---

## 🗄️ 5. Database Setup (PostgreSQL)

### 5.1 Create Database & User

```bash
sudo -u postgres psql
CREATE DATABASE tv_shows_db;
CREATE USER myuser WITH PASSWORD 'mypassword';
GRANT ALL PRIVILEGES ON DATABASE tv_shows_db TO myuser;
\q
```

### 5.2 Create Tables

```bash
./venv/bin/python - <<'PY'
from tv_app.app import app
from tv_app.models import db

with app.app_context():
    db.create_all()

print("Tables created successfully")
PY
```

### 5.3 Hybrid Index & Smart Search

```bash
sudo -u postgres psql -d tv_shows_db -c "
DROP INDEX IF EXISTS ix_tv_shows_tmdb_id;
CREATE UNIQUE INDEX IF NOT EXISTS ix_tmdb_category
ON tv_shows (tmdb_id, category);

CREATE EXTENSION IF NOT EXISTS pg_trgm;
"
```

---

## ⚙️ 6. Process Management (Supervisor)

Create configuration:

```bash
nano /etc/supervisor/conf.d/ibox-tv.conf
```

```ini
[program:ibox-gunicorn]
directory=/root/tvweb
command=/root/tvweb/venv/bin/gunicorn -w 3 -b 127.0.0.1:8000 tv_app.app:app
user=root
autostart=true
autorestart=true
stdout_logfile=/var/log/ibox/gunicorn.out.log
stderr_logfile=/var/log/ibox/gunicorn.err.log
environment=PYTHONPATH="/root/tvweb"

[program:ibox-celery]
directory=/root/tvweb
command=/root/tvweb/venv/bin/celery -A tv_app.tasks worker --loglevel=INFO --concurrency=2
user=root
autostart=true
autorestart=true
stdout_logfile=/var/log/ibox/celery.out.log
stderr_logfile=/var/log/ibox/celery.err.log
environment=PYTHONPATH="/root/tvweb"

[program:ibox-celerybeat]
directory=/root/tvweb
command=/root/tvweb/venv/bin/celery -A tv_app.tasks beat --loglevel=INFO
user=root
autostart=true
autorestart=true
stdout_logfile=/var/log/ibox/celerybeat.out.log
stderr_logfile=/var/log/ibox/celerybeat.err.log
environment=PYTHONPATH="/root/tvweb"
```

### Start Services

```bash
sudo mkdir -p /var/log/ibox
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl restart all
```

---

## 🌐 7. Nginx Setup (Multi-Domain)

**File:** `/etc/nginx/sites-available/one.ibox-tv.com`

```nginx
server {
    listen 80;
    server_name ibox-tv.com www.ibox-tv.com anime.ibox-tv.com movies.ibox-tv.com;

    location / {
        include proxy_params;
        proxy_pass http://127.0.0.1:8000;
    }

    location /static {
        alias /root/tvweb/tv_app/static;
        expires 30d;
        add_header Cache-Control "public, max-age=2592000";
    }
}
```

### Enable & Secure

```bash
sudo ln -s /etc/nginx/sites-available/one.ibox-tv.com /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx --expand \
  -d ibox-tv.com -d www.ibox-tv.com \
  -d anime.ibox-tv.com -d movies.ibox-tv.com
```

---

## 🧑‍💻 8. Operations Manual

### ☢️ The Nuke Panel

Access: `https://ibox-tv.com/nuke`

**Controls:**
- **Start** — Begin infinite MongoDB backfill
- **Pause** — Stop after current batch
- **Reset** — Clear Redis checkpoint
- **Purge** — ⚠️ Delete all movie records

---

## 🛠️ Troubleshooting

| Problem | Solution |
|-------|---------|
| Search shows `0` | Ensure `pg_trgm` is enabled |
| `No such column: category` | Recreate tables |
| Backfill slow / stops | Add more TMDb tokens |
| Locked out of Nuke | `redis-cli DEL nuke:enabled` |

---

## ✅ Deployment Complete

Your **iBOX TV Hybrid Platform** is live, scalable, and ready for continuous ingestion 🚀

