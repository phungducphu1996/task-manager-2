# Vikunja Migration Runbook

This runbook migrates Hazel Task Manager toward Vikunja as the task UI/source-of-truth while keeping the existing FastAPI backend as the Hazel Bridge for Zalo bot, notifications, reminders, and legacy archive.

## Phase: Vikunja Staging On VPS

Staging routes:

- Vikunja UI/API: `https://hazeleo.com/vikunja/`
- Legacy Task Manager: `https://hazeleo.com/task/`
- Hazel Bridge API: `https://hazeleo.com/task-api/`

This phase uses the official Docker image `vikunja/vikunja`. Do not clone or build Vikunja source for staging.

Deploy assets live in:

```text
deploy/vikunja/
```

## 1. Prepare `/opt/vikunja`

On VPS, after pulling this repository into `/opt/task-manager`:

```bash
sudo mkdir -p /opt/vikunja/files
sudo chown -R 1000:1000 /opt/vikunja/files
sudo cp /opt/task-manager/deploy/vikunja/docker-compose.yml /opt/vikunja/docker-compose.yml
sudo cp /opt/task-manager/deploy/vikunja/.env.example /opt/vikunja/.env
sudo nano /opt/vikunja/.env
```

Fill Supabase Postgres values in `/opt/vikunja/.env`.

Important:

- Use a database/schema dedicated to Vikunja. If using one Supabase database, set `VIKUNJA_DATABASE_SCHEMA=vikunja` and create that schema first.
- Do not use the legacy `teamtask_manager` schema for Vikunja migrations.
- Keep `VIKUNJA_SERVICE_PUBLICURL=https://hazeleo.com/vikunja/` with the trailing slash.

## 2. Start Vikunja

```bash
cd /opt/vikunja
sudo docker compose pull
sudo docker compose up -d
sudo docker compose logs -f vikunja
```

Local check:

```bash
curl -I http://127.0.0.1:3456/
```

## 3. Add Nginx Staging Route

Paste the contents of:

```text
/opt/task-manager/deploy/vikunja/nginx-location.conf
```

inside the existing `server { ... }` block for `hazeleo.com`.

Then reload nginx:

```bash
sudo nginx -t && sudo systemctl reload nginx
curl -I https://hazeleo.com/vikunja/
```

Keep existing `/task/` and `/task-api/` locations unchanged.

## 4. Initial Vikunja Setup

Open:

```text
https://hazeleo.com/vikunja/
```

Create the first admin account.

Create local users matching active `social.users.username` values, for example:

```text
admin
hazelnguyen
quynhanh
shindang
```

Create project:

```text
Hazel Task Manager
```

Create Kanban buckets:

```text
Inbox
Todo
Doing
Review
Ready
Done/Logbook
```

After users are created, disable open registration:

```bash
sudo sed -i 's/^VIKUNJA_SERVICE_ENABLEREGISTRATION=.*/VIKUNJA_SERVICE_ENABLEREGISTRATION=false/' /opt/vikunja/.env
cd /opt/vikunja && sudo docker compose up -d
```

## 5. Configure Hazel Bridge

Create a Vikunja API token from the admin account.

Copy values from:

```text
/opt/task-manager/deploy/vikunja/task-manager-env.example
```

into:

```text
/opt/task-manager/backend/.env
```

Minimum env values:

```bash
VIKUNJA_API_URL=https://hazeleo.com/vikunja
VIKUNJA_API_TOKEN=replace-with-vikunja-api-token
VIKUNJA_PUBLIC_URL=https://hazeleo.com/vikunja
VIKUNJA_TASK_URL_TEMPLATE=https://hazeleo.com/vikunja/projects/{project_id}/tasks/{task_id}
VIKUNJA_PROJECT_TITLE=Hazel Task Manager
VIKUNJA_PROJECT_ID=
VIKUNJA_BUCKET_INBOX_ID=
VIKUNJA_BUCKET_TODO_ID=
VIKUNJA_BUCKET_DOING_ID=
VIKUNJA_BUCKET_REVIEW_ID=
VIKUNJA_BUCKET_READY_ID=
VIKUNJA_BUCKET_DONE_ID=
```

Restart backend:

```bash
sudo systemctl restart taskmanager-api.service
journalctl -u taskmanager-api.service -f
```

## 6. Bridge Smoke Tests

Use the helper:

```bash
cd /opt/task-manager
NOTIFY_INTERNAL_TOKEN='your-token' bash deploy/vikunja/smoke-test.sh
```

Or run manually:

```bash
TOKEN="$NOTIFY_INTERNAL_TOKEN"

curl -s https://hazeleo.com/task-api/internal/vikunja/status \
  -H "X-Internal-Token: $TOKEN" | jq

curl -s -X POST https://hazeleo.com/task-api/internal/vikunja/sync-users \
  -H "X-Internal-Token: $TOKEN" | jq

curl -s -X POST 'https://hazeleo.com/task-api/internal/vikunja/migrate-tasks?dry_run=true&limit=10' \
  -H "X-Internal-Token: $TOKEN" | jq
```

Do not run live migration until user mappings look correct.

## 7. Live Migration

Small batch first:

```bash
curl -s -X POST 'https://hazeleo.com/task-api/internal/vikunja/migrate-tasks?limit=5' \
  -H "X-Internal-Token: $TOKEN" | jq
```

If the first tasks look right in Vikunja, run the full migration:

```bash
curl -s -X POST 'https://hazeleo.com/task-api/internal/vikunja/migrate-tasks' \
  -H "X-Internal-Token: $TOKEN" | jq
```

Verify:

- migrated count matches legacy task count
- assignee/due/priority look right
- Kanban bucket mapping is correct
- comments and attachment links are preserved or documented in description
- old `/task/` still works
- `/task-api/health` still returns `ok`

## 8. Webhook Later

Point Vikunja webhook to:

```text
https://hazeleo.com/task-api/vikunja/webhook
```

Header:

```text
X-Vikunja-Secret: $VIKUNJA_WEBHOOK_SECRET
```

V1 records webhook payloads. Enforcement/reconciliation can be expanded after real Vikunja event payloads are observed.

## 8.1. Realtime Notification Reconcile

After tasks are migrated and `VIKUNJA_PROJECT_ID` is configured, seed the first Vikunja snapshot before enabling cron/webhooks. The first reconcile only stores baseline state and does not notify everyone.

```bash
TOKEN="$NOTIFY_INTERNAL_TOKEN"

curl -s -X POST https://hazeleo.com/task-api/internal/vikunja/reconcile \
  -H "X-Internal-Token: $TOKEN" | jq
```

Then add a VPS cron as a safety net. This catches task changes even if a Vikunja webhook is missed:

```bash
* * * * * curl -s -X POST https://hazeleo.com/task-api/internal/vikunja/reconcile -H "X-Internal-Token: $NOTIFY_INTERNAL_TOKEN" >/dev/null 2>&1
```

## 8.2. Zalo Bot Task Source

When `VIKUNJA_API_URL`, `VIKUNJA_API_TOKEN`, and `VIKUNJA_PROJECT_ID` are configured, the Zalo bot task tools use the new Task Manager source for:

- `find_tasks`
- `list_tasks`
- `create_task`
- `approve_task`
- `update_task_status`
- `update_task_fields`

Legacy task tables remain as fallback only when the new Task Manager env is not configured.

Status changes need bucket IDs in `/opt/task-manager/backend/.env`; otherwise the bot will refuse the status update instead of claiming success:

```bash
VIKUNJA_BUCKET_INBOX_ID=
VIKUNJA_BUCKET_TODO_ID=
VIKUNJA_BUCKET_DOING_ID=
VIKUNJA_BUCKET_REVIEW_ID=
VIKUNJA_BUCKET_READY_ID=
VIKUNJA_BUCKET_DONE_ID=
```

Realtime notification rules currently handled by reconcile/webhook:

- new assignee on an existing Vikunja task -> notify the new assignee
- status changes to `review` -> notify admins
- `review -> ready` -> notify assignees
- status changes to `done` -> notify admins
- title/due date changes -> notify assignees

Daily morning/evening notification jobs and Reminder Engine daily summaries read from Vikunja when `VIKUNJA_API_URL`, `VIKUNJA_API_TOKEN`, and `VIKUNJA_PROJECT_ID` are configured. If Vikunja API fails, they fall back to legacy task tables.

## 9. Cutover Later

After staging smoke test and migration verification:

- move current app from `/task/` to `/task-legacy/`
- route `/task/` to Vikunja
- keep `/task-api/` as Hazel Bridge

Rollback is nginx-only: point `/task/` back to legacy frontend and keep bridge running.
