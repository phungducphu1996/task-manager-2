# Team Task Manager Backend

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8010 --reload
```

API docs: http://localhost:8010/docs

If `8010` is also busy on your VPS, change to any free port:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8100
```

## Notes for Supabase

Use your Supabase Postgres connection string in `DATABASE_URL`, e.g.

`postgresql+psycopg://postgres:<password>@db.<project-ref>.supabase.co:5432/postgres`

If you share one database for multiple apps, set `DB_SCHEMA` (for example `teamtask`) so this app keeps tables isolated.

## Zalo Notification V1

The backend supports:
- Realtime notification events from task flow (`create assigned`, `review -> ready`, `* -> review`, `member -> done`).
- Daily jobs via internal endpoint:
  - `POST /internal/notifications/run?job=morning`
  - `POST /internal/notifications/run?job=evening`

Required env:
- `NOTIFY_INTERNAL_TOKEN`
- `ZALO_WORKER_URL` (`http://127.0.0.1:8787` or full `.../api/send-text`)
- `ZALO_WORKER_TOKEN` (optional if worker doesn't require auth)
- `ZALO_SHARED_SECRET` (for worker `X-Internal-Secret` auth)
- `ZALO_GROUP_ID`

Example crontab (server-side, Asia/Ho_Chi_Minh):

```bash
0 9 * * * curl -sS -X POST "http://127.0.0.1:8010/internal/notifications/run?job=morning" -H "X-Internal-Token: ${NOTIFY_INTERNAL_TOKEN}"
0 18 * * * curl -sS -X POST "http://127.0.0.1:8010/internal/notifications/run?job=evening" -H "X-Internal-Token: ${NOTIFY_INTERNAL_TOKEN}"
```
