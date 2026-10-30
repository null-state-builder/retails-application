#!/bin/sh
# The goods worker shares the API's /data volume, so both run here.
# The worker restarts itself if it stops; the API stays in the foreground.
# More than one API worker so a CPU-heavy read cannot starve every other request.
(while true; do python manage.py run_goods_worker; echo "goods worker stopped; restarting in 5s" >&2; sleep 5; done) &
exec uvicorn server:app --host 0.0.0.0 --port "${PORT:-8000}" --workers "${WEB_CONCURRENCY:-2}" --proxy-headers --forwarded-allow-ips='*'
