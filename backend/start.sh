#!/bin/sh
# Railway start: the goods worker shares the API's /data volume, so both run here.
# The worker restarts itself if it stops; the API stays in the foreground.
(while true; do python manage.py run_goods_worker; echo "goods worker stopped; restarting in 5s" >&2; sleep 5; done) &
exec uvicorn server:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips='*'
