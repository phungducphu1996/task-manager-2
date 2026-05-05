# Vikunja Staging Deploy Pack

This pack runs the official `vikunja/vikunja` Docker image on the VPS and exposes it at:

```text
https://hazeleo.com/vikunja/
```

The old Task Manager stays at `/task/`; Hazel Bridge stays at `/task-api/`.

## 1. Copy Files On VPS

From `/opt/task-manager` after `git pull`:

```bash
sudo mkdir -p /opt/vikunja/files
sudo chown -R 1000:1000 /opt/vikunja/files
sudo cp /opt/task-manager/deploy/vikunja/docker-compose.yml /opt/vikunja/docker-compose.yml
sudo cp /opt/task-manager/deploy/vikunja/.env.example /opt/vikunja/.env
sudo nano /opt/vikunja/.env
```

Fill Supabase Postgres values in `/opt/vikunja/.env`. Use a database/schema dedicated to Vikunja. If using one Supabase database, set `VIKUNJA_DATABASE_SCHEMA=vikunja` and create that schema first.

## 2. Start Vikunja

```bash
cd /opt/vikunja
sudo docker compose pull
sudo docker compose up -d
sudo docker compose logs -f vikunja
```

Quick local check:

```bash
curl -I http://127.0.0.1:3456/
```

## 3. Add Nginx Route

Paste `deploy/vikunja/nginx-location.conf` into the existing `server { ... }` block for `hazeleo.com`, then:

```bash
sudo nginx -t && sudo systemctl reload nginx
curl -I https://hazeleo.com/vikunja/
```

## 4. Initial Vikunja Setup

Open `https://hazeleo.com/vikunja/` and create the first admin user.

Create local users matching `social.users.username`, for example:

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

## 5. Connect Hazel Bridge

Create an API token from the Vikunja admin account, then copy values from:

```text
/opt/task-manager/deploy/vikunja/task-manager-env.example
```

into:

```text
/opt/task-manager/backend/.env
```

Restart Bridge:

```bash
sudo systemctl restart taskmanager-api.service
journalctl -u taskmanager-api.service -f
```

## 6. Smoke Test

```bash
cd /opt/task-manager
NOTIFY_INTERNAL_TOKEN='your-token' bash deploy/vikunja/smoke-test.sh
```

Only run live migration after dry-run payloads and user mappings look right.

## 7. Live Migration, Small Batch First

```bash
TOKEN='your-token'

curl -s -X POST 'https://hazeleo.com/task-api/internal/vikunja/migrate-tasks?limit=5' \
  -H "X-Internal-Token: $TOKEN" | jq
```

If the first 5 look good in Vikunja, run the full migration:

```bash
curl -s -X POST 'https://hazeleo.com/task-api/internal/vikunja/migrate-tasks' \
  -H "X-Internal-Token: $TOKEN" | jq
```
