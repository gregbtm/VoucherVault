---
name: verify
description: Build, run, and drive VoucherVault to observe a change working end-to-end (Django app + DRF API + Celery notify task).
---

# Verifying VoucherVault

## Build & run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install --quiet -r requirements.txt   # ~1-2 min, no output on success

rm -f database/db.sqlite3
DB_ENGINE=sqlite3 python manage.py migrate

# seed a couple of users
echo "from django.contrib.auth.models import User; \
User.objects.filter(username__in=['alice','bob']).delete(); \
User.objects.create_user('alice','a@example.com','AlicePw123!'); \
User.objects.create_user('bob','b@example.com','BobPw123!')" \
  | DB_ENGINE=sqlite3 python manage.py shell

DB_ENGINE=sqlite3 DEBUG=True nohup python manage.py runserver 127.0.0.1:8000 > /tmp/vv_server.log 2>&1 &
sleep 3
```

Web UI is locale-prefixed: `http://127.0.0.1:8000/en/...` (not bare `/`).
API is not: `http://127.0.0.1:8000/api/v1/...`.

## Drive the API surface

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/v1/auth/token/ \
  -d "username=alice&password=AlicePw123!" | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

# unauth check
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/v1/items/   # expect 401

curl -s -H "Authorization: Token $TOKEN" http://127.0.0.1:8000/api/v1/items/
```
Swagger UI: `/api/v1/docs/`, schema: `/api/v1/schema/` (should be 200, no CDN — sidecar-served).

## Drive the web UI (session auth)

```bash
COOKIES=/tmp/vv_cookies.txt
CSRF=$(curl -s -c $COOKIES http://127.0.0.1:8000/en/accounts/login/ \
  | grep -o 'name="csrfmiddlewaretoken" value="[^"]*"' | head -1 | sed 's/.*value="//;s/"//')
curl -s -b $COOKIES -c $COOKIES -X POST http://127.0.0.1:8000/en/accounts/login/ \
  -d "username=alice&password=AlicePw123!&csrfmiddlewaretoken=$CSRF" \
  -H "Referer: http://127.0.0.1:8000/en/accounts/login/" -o /dev/null -w "%{http_code}\n"
# reuse $COOKIES for subsequent GETs/POSTs; grab a fresh csrfmiddlewaretoken per form page before each POST
```

Key pages: `/en/` (inventory), `/en/items/create/`, `/en/wallets/`, `/en/tags/`,
`/en/notifications/` (rules), `/en/notifications/log/`.

## Drive the Celery notify task (no broker needed for verification)

```bash
source .venv/bin/activate
DJANGO_SETTINGS_MODULE=myproject.settings DB_ENGINE=sqlite3 python manage.py shell -c "
from notify.tasks import check_and_notify_expiry
check_and_notify_expiry()
from notify.models import NotificationLog
for log in NotificationLog.objects.order_by('sent_at'):
    print(log.event_type, log.item, log.rule, log.success, log.detail[:60])
"
```
Run it twice in a row — a rule must NOT re-fire for the same (item, event_type)
after a successful send (dedup via NotificationLog). ntfy backend can be
pointed at the real public `https://ntfy.sh` with a throwaway topic for a
genuine end-to-end send (subscribe in the ntfy app/web to see it arrive).

## Gotchas

- `LANGUAGE_CODE` is `'en'` (fixed from a latent `'en-us'` mismatch bug in
  Phase 2 — if it regresses, the very first `reverse()` call in a fresh
  process can 404 before any request activates a supported locale).
- ntfy `Title` header must be UTF-8 **bytes**, not `str` — `requests`
  latin-1-encodes plain str headers and crashes on emoji/non-ASCII titles.
- Every queryset in `api/` and every notify-app view/queryset is scoped to
  `request.user` — when verifying a new endpoint, always add a two-user
  cross-access check (second user gets 404, not 403, on someone else's object).
- No Celery worker/broker is required to verify task *logic* — call the task
  function directly via `manage.py shell` as above. Only use a real worker if
  specifically verifying Celery Beat scheduling/queue wiring.
- Clean up after: `pkill -f "manage.py runserver 127.0.0.1:8000"`, then
  `rm -rf .venv database/db.sqlite3` — neither should ever be committed.
