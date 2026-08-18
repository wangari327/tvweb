web: gunicorn "tv_app.app:app" --workers ${WEB_CONCURRENCY:-2} --bind 0.0.0.0:${PORT:-8000} --timeout 30 --graceful-timeout 10 --keep-alive 5 --max-requests 500 --max-requests-jitter 50
worker: celery -A tv_app.tasks worker --loglevel=INFO --concurrency=${CELERY_CONCURRENCY:-1} --max-tasks-per-child=1000
beat: celery -A tv_app.tasks beat --loglevel=INFO --schedule=/tmp/ibox-tv-celerybeat-schedule
