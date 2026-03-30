# Task Manager Playwright Workflow

Reliable CLI-first browser automation for this app using the Playwright wrapper skill.

## What It Automates

1. Open login page.
2. Login (or reuse an existing authenticated session).
3. Create a task via quick add.
4. Open the new task and verify detail panel.
5. Switch to Inbox and verify the view.
6. Save artifacts:
- `output/playwright/task-manager-workflow/final.png`
- `output/playwright/task-manager-workflow/trace.zip`

## Prerequisites

1. `npx` available on your machine.
2. Frontend and backend running locally.

Backend:

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8010 --reload
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

## Run

From repo root:

```bash
chmod +x output/playwright/task-manager-workflow/run_workflow.sh
./output/playwright/task-manager-workflow/run_workflow.sh
```

## Optional Environment Variables

```bash
APP_URL=http://127.0.0.1:5173/login
LOGIN_USERNAME=trang
LOGIN_PASSWORD=trang123
PLAYWRIGHT_SESSION=task-manager-workflow
TASK_TITLE="PW Smoke Manual Title"
HEADED=1
PWCLI="$HOME/.codex/skills/playwright/scripts/playwright_cli.sh"
```

Example:

```bash
LOGIN_USERNAME=trang LOGIN_PASSWORD=trang123 HEADED=1 \
./output/playwright/task-manager-workflow/run_workflow.sh
```

## Notes

- If first run fails on npm registry connectivity, rerun where network access to `registry.npmjs.org` is available.
- The script is selector-based (not element id snapshot refs), so it is more stable across UI layout changes.
