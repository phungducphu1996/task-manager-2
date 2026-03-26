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
