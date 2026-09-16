import os
from dotenv import load_dotenv
from celery.schedules import crontab

load_dotenv()

broker_url = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
result_backend = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')  # Use the same Redis

# Configure Celery Beat's schedule
beat_schedule = {
    # --- TV SHOW UPDATE: Updated to 10 Minutes ---
    'update-tv-shows-every-10-minutes': {
        'task': 'tv_app.tasks.update_tv_shows',
        'schedule': crontab(minute='*/10'),
    },
    # --- MOVIE SYNC: Updated to 1 Hour ---
    'sync-movies-every-hour': {
        'task': 'tv_app.tasks.sync_movies',
        'schedule': crontab(minute=0),
    },
    'refresh-genre-hubs-every-6-hours': {
        'task': 'tv_app.tasks.refresh_genre_hubs',
        'schedule': crontab(minute=15, hour='*/6'),
    },
    'reset-clicks-every-12-hours': {
        'task': 'tv_app.tasks.reset_clicks',
        'schedule': crontab(minute=0, hour='*/12'),
    },
}
broker_connection_retry_on_startup = True
