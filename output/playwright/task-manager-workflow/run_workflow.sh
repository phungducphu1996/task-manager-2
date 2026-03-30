#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1"
}

if ! command -v npx >/dev/null 2>&1; then
  echo "Error: npx is required but was not found on PATH." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

export CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
export PWCLI="${PWCLI:-$CODEX_HOME/skills/playwright/scripts/playwright_cli.sh}"

if [[ ! -x "$PWCLI" ]]; then
  echo "Error: Playwright wrapper not found or not executable at: $PWCLI" >&2
  exit 1
fi

export APP_URL="${APP_URL:-http://127.0.0.1:5173/login}"
export LOGIN_USERNAME="${LOGIN_USERNAME:-trang}"
export LOGIN_PASSWORD="${LOGIN_PASSWORD:-trang123}"
export PLAYWRIGHT_SESSION="${PLAYWRIGHT_SESSION:-task-manager-workflow}"
export HEADED="${HEADED:-0}"
export TASK_TITLE="${TASK_TITLE:-PW Smoke $(date '+%Y%m%d-%H%M%S')}"

ARTIFACT_DIR="$SCRIPT_DIR"
mkdir -p "$ARTIFACT_DIR"
export SCREENSHOT_PATH="$ARTIFACT_DIR/final.png"
export TRACE_PATH="$ARTIFACT_DIR/trace.zip"

pw() {
  "$PWCLI" --session "$PLAYWRIGHT_SESSION" "$@"
}

pw_run_code() {
  local code="$1"
  pw run-code "$code"
}

cleanup() {
  pw close >/dev/null 2>&1 || true
}
trap cleanup EXIT

log "Resetting existing browser session (if any)"
cleanup

log "Opening $APP_URL"
if [[ "$HEADED" == "1" ]]; then
  pw open "$APP_URL" --headed
else
  pw open "$APP_URL"
fi

log "Capturing initial snapshot"
pw snapshot >/dev/null

log "Starting trace capture"
pw_run_code "$(cat <<'JS'
await page.context().tracing.start({ screenshots: true, snapshots: true });
console.log("trace:start");
JS
)"

log "Logging in (or reusing active auth state)"
pw_run_code "$(cat <<'JS'
await page.waitForLoadState("domcontentloaded");

const loginFormCount = await page.locator("form.login-form").count();
if (loginFormCount > 0) {
  await page.fill('input[autocomplete="username"]', process.env.LOGIN_USERNAME ?? "");
  await page.fill('input[autocomplete="current-password"]', process.env.LOGIN_PASSWORD ?? "");
  await Promise.all([
    page.waitForURL(/\/today(?:[/?#].*)?$/, { timeout: 30000 }),
    page.click("button.login-btn")
  ]);
} else {
  await page.waitForURL(/\/(today|inbox|upcoming|anytime|logbook|review)(?:[/?#].*)?$/, { timeout: 30000 });
}

if (!/\/today(?:[/?#].*)?$/.test(new URL(page.url()).pathname)) {
  await page.click('[data-testid="nav-item-today"]');
  await page.waitForURL(/\/today(?:[/?#].*)?$/, { timeout: 15000 });
}

const heading = (await page.textContent(".main-view-title"))?.trim();
if (heading !== "Today") {
  throw new Error(`Expected Today heading, got: ${heading ?? "<null>"}`);
}
console.log("login:ok");
JS
)"

log "Creating quick task: $TASK_TITLE"
pw_run_code "$(cat <<'JS'
const taskTitle = process.env.TASK_TITLE ?? "";
if (!taskTitle) {
  throw new Error("TASK_TITLE is empty");
}

await page.waitForSelector("input.quick-composer-input", { timeout: 20000 });
await page.fill("input.quick-composer-input", `${taskTitle} !high today`);
await page.keyboard.press("Enter");

await page.waitForFunction(
  (title) => {
    const rows = Array.from(document.querySelectorAll(".task-row .task-title"));
    return rows.some((node) => (node.textContent || "").includes(title));
  },
  taskTitle,
  { timeout: 20000 }
);
console.log("quick-add:ok");
JS
)"

log "Opening created task and verifying detail panel"
pw_run_code "$(cat <<'JS'
const taskTitle = process.env.TASK_TITLE ?? "";
const row = page.locator(".task-row", { hasText: taskTitle }).first();
await row.waitFor({ state: "visible", timeout: 20000 });
await row.click();

await page.waitForFunction(
  (title) => {
    const heading = document.querySelector(".detail-header h2");
    return Boolean(heading && (heading.textContent || "").includes(title));
  },
  taskTitle,
  { timeout: 20000 }
);
console.log("detail-panel:ok");
JS
)"

log "Navigating to Inbox and validating view switch"
pw_run_code "$(cat <<'JS'
await page.click('[data-testid="nav-item-inbox"]');
await page.waitForURL(/\/inbox(?:[/?#].*)?$/, { timeout: 15000 });

const heading = (await page.textContent(".main-view-title"))?.trim();
if (heading !== "Inbox") {
  throw new Error(`Expected Inbox heading, got: ${heading ?? "<null>"}`);
}

const groupTitles = await page.$$eval(
  ".task-group .task-group-header h3",
  (nodes) => nodes.map((node) => (node.textContent || "").trim()).filter(Boolean)
);
console.log(`inbox-groups:${groupTitles.length ? groupTitles.join("|") : "none"}`);
JS
)"

log "Capturing final screenshot and trace"
pw_run_code "$(cat <<'JS'
await page.screenshot({ path: process.env.SCREENSHOT_PATH, fullPage: true });
await page.context().tracing.stop({ path: process.env.TRACE_PATH });
console.log("artifacts:ok");
JS
)"

log "Workflow completed"
echo "Task title: $TASK_TITLE"
echo "Screenshot: $SCREENSHOT_PATH"
echo "Trace: $TRACE_PATH"
