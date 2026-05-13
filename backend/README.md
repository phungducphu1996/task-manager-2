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
- `ZALO_ALLOWED_GROUP_IDS` (comma-separated group ids that may send commands)
- `ZALO_BOT_ALIASES` (default `@TaskBot,@task`)
- `ZALO_BOT_USER_IDS` (comma-separated Zalo ids for native mention detection)
- `TASK_PUBLIC_BASE_URL` (for example `https://hazeleo.com/task`)

Example crontab (server-side, Asia/Ho_Chi_Minh):

```bash
0 9 * * * curl -sS -X POST "http://127.0.0.1:8010/internal/notifications/run?job=morning" -H "X-Internal-Token: ${NOTIFY_INTERNAL_TOKEN}"
0 18 * * * curl -sS -X POST "http://127.0.0.1:8010/internal/notifications/run?job=evening" -H "X-Internal-Token: ${NOTIFY_INTERNAL_TOKEN}"
```

## Gmail Sale & Message Monitor

The backend can read Gmail with IMAP app password, detect Etsy sale and conversation emails, and send Zalo group notifications through the existing notification pipeline.

Required env:
- `GMAIL_ADDRESS`
- `GMAIL_APP_PASSWORD`
- `GMAIL_IMAP_HOST` (default `imap.gmail.com`)
- `GMAIL_IMAP_PORT` (default `993`)
- `GMAIL_IMAP_MAILBOX` (default `INBOX`)
- `GMAIL_SEARCH_SINCE_DAYS` (default `7`)
- `GMAIL_SALE_FROM_ADDRESSES` (default `transaction@etsy.com`)
- `GMAIL_SALE_SUBJECT` (default `You made a sale on Etsy`)
- `GMAIL_MESSAGE_FROM_ADDRESSES` (default `no-reply@account.etsy.com,conversations@mail.etsy.com`)
- `GMAIL_POLL_MAX_RESULTS` (default `10`)
- `ZALO_GROUP_ID`

Internal jobs:

```bash
*/2 * * * * curl -sS -X POST "http://127.0.0.1:8010/internal/gmail/poll" -H "X-Internal-Token: ${NOTIFY_INTERNAL_TOKEN}"
0 18 * * * curl -sS -X POST "http://127.0.0.1:8010/internal/gmail/digest" -H "X-Internal-Token: ${NOTIFY_INTERNAL_TOKEN}"
```

## Zalo Group Commands V1

The `zalo-worker` listener can forward group messages to `POST /zalo/incoming`. The backend handles messages that start with a configured bot alias, or native mention payloads that match `ZALO_BOT_USER_IDS` / `ZALO_BOT_ALIASES`.

```text
@TaskBot add Fix mockup @quang #AmzMage type:Design due:tomorrow !high
@TaskBot list today
@TaskBot list inbox
```

Incoming commands are authenticated with `X-Internal-Secret: <ZALO_SHARED_SECRET>`, deduped by Zalo `message_id`, and mapped to `social.users.zalo_user_id`.

## Zalo Office Copilot Phase 1

The same `POST /zalo/incoming` route now supports free-form chat when a message starts with a bot alias but is not an `add/list` command.

Example:

```text
@TaskBot hôm nay em còn task gì chưa xong?
@TaskBot em thích trà sữa ít ngọt nha, nhớ giúp em
@TaskBot nhớ là tuần sau team mình họp toàn team sáng thứ hai
```

Phase 1 includes:
- Persona markdown file: `backend/bot/persona/core.md`
- Per-user markdown profiles: `backend/bot/profiles/*.md`
- Recent conversation memory in DB (`bot_conversation_messages`)
- Lightweight fact memory in DB (`bot_memory_facts`)
- Office events markdown log: `backend/bot/events.md`
- Task-aware chat context using the current Task Manager data

Required env for AI mode:
- `BOT_ENABLED=true`
- `OPENAI_API_KEY`
- `BOT_LLM_MODEL` (default `gpt-4.1-mini`)
- `BOT_LLM_BASE_URL` (default `https://api.openai.com/v1`)

If `OPENAI_API_KEY` is missing, the bot still replies in a basic fallback mode instead of failing.
