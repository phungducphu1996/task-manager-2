# Team Task Manager v1

Things-style team task manager with Python API + Vue frontend + Supabase Postgres.

## Stack

- Backend: FastAPI, SQLAlchemy, Alembic
- Frontend: Vue 3, Vite, Pinia, Vue Router
- Database: Supabase Postgres (or local Postgres for dev)

## Run backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8010 --reload
```

## Run frontend

```bash
cd frontend
npm install
cp .env.example .env
npm run dev
```

If backend is not on `8000`, set API URL in frontend `.env`:

```bash
VITE_API_URL=http://<your-vps-ip>:8010
```

## Tests

Backend:

```bash
cd backend
pytest
```

Frontend:

```bash
cd frontend
npm test
```
