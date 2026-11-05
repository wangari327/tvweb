import os
from dotenv import load_dotenv
from celery.schedules import crontab

load_dotenv()

broker_url = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
result_backend = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')  # Use the same Redis

# Keep the fast Telegram importer independent from the comparatively expensive
# movie scan.  A slow metadata lookup must never prevent the catalogue from
# receiving a new channel post.
task_default_queue = 'maintenance'
task_routes = {
    'tv_app.tasks.update_tv_shows': {'queue': 'ingestion'},
    'tv_app.tasks.worker_heartbeat': {'queue': 'ingestion'},
    'tv_app.tasks.sync_movies': {'queue': 'movies'},
    'tv_app.tasks.backfill_movies_task': {'queue': 'movies'},
    'tv_app.tasks.hard_reset_backfill': {'queue': 'movies'},
    'tv_app.tasks.refresh_genre_hubs': {'queue': 'maintenance'},
    'tv_app.tasks.reset_clicks': {'queue': 'maintenance'},
}

# Do not reserve a pile of periodic work in one worker.  If the process is
# restarted, work stays in Redis and can be handled by the replacement.
task_acks_late = True
worker_prefetch_multiplier = 1

# Configure Celery Beat's schedule
beat_schedule = {
    # --- TV SHOW UPDATE: Updated to 10 Minutes ---
    'update-tv-shows-every-10-minutes': {
        'task': 'tv_app.tasks.update_tv_shows',
        'schedule': crontab(minute='*/10'),
        'options': {'queue': 'ingestion', 'expires': 540},
    },
    # A separate heartbeat lets an OS-level watchdog detect a living Celery
    # parent whose worker child has stopped consuming Redis messages.
    'worker-heartbeat-every-two-minutes': {
        'task': 'tv_app.tasks.worker_heartbeat',
        'schedule': crontab(minute='*/2'),
        'options': {'queue': 'ingestion', 'expires': 300},
    },
    # --- MOVIE SYNC: Updated to 1 Hour ---
    'sync-movies-every-hour': {
        'task': 'tv_app.tasks.sync_movies',
        'schedule': crontab(minute=0),
        'options': {'queue': 'movies', 'expires': 3300},
    },
    'refresh-genre-hubs-every-6-hours': {
        'task': 'tv_app.tasks.refresh_genre_hubs',
        'schedule': crontab(minute=15, hour='*/6'),
        'options': {'queue': 'maintenance', 'expires': 21000},
    },
    'reset-clicks-every-12-hours': {
        'task': 'tv_app.tasks.reset_clicks',
        'schedule': crontab(minute=0, hour='*/12'),
        'options': {'queue': 'maintenance', 'expires': 42600},
    },
}
broker_connection_retry_on_startup = True
