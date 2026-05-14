from __future__ import annotations

import base64
from datetime import date, datetime, time
import hmac
from logging import getLogger
from os.path import basename
from re import sub
from typing import Any, Literal
from urllib.parse import unquote, urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, inspect, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from .auth import AuthError, create_access_token, decode_access_token, extract_bearer_token, verify_password
from .bot_files import ensure_bot_files, ensure_notification_event_prompt
from .config import get_settings
from .database import Base, engine, get_db
from .models import (
    BotConversationMessage,
    BotConversationState,
    BotMemoryFact,
    GmailMonitorEvent,
    IntegrationConfig,
    NotificationChannel,
    NotificationDelivery,
    NotificationEvent,
    NotificationStatus,
    ReminderInteraction,
    ReminderRule,
    ReminderRun,
    VikunjaBridgeState,
    VikunjaTaskMapping,
    VikunjaUserMapping,
    Shop,
    Subtask,
    Task,
    TaskAttachment,
    TaskComment,
    TaskStatus,
    TaskType,
    User,
    ZaloIncomingCommand,
)
from .notifications import (
    NotificationSpec,
    enqueue_task_created_notifications,
    enqueue_task_deleted_notifications,
    enqueue_task_status_transition_notifications,
    enqueue_task_updated_notifications,
    dispatch_due_notification_events,
    enqueue_notification_event,
    is_internal_token_valid,
    run_daily_notification_job,
    send_zalo_text,
)
from .gmail_monitor import GMAIL_ZALO_CONFIG_KEY, GmailMonitorError, gmail_zalo_config, poll_gmail_and_notify, run_gmail_daily_digest
from .schemas import (
    LoginRequest,
    LoginResponse,
    ShopCreate,
    ShopOut,
    ShopUpdate,
    SubtaskCreate,
    SubtaskOut,
    SubtaskUpdate,
    TaskAttachmentOut,
    TaskAttachmentLinkCreate,
    TaskCommentCreate,
    TaskCommentOut,
    TaskConvertRequest,
    TaskCreate,
    TaskFullEdit,
    TaskFullEditOut,
    TaskListResponse,
    TaskOut,
    TaskReorderRequest,
    TaskStatusUpdate,
    TaskTypeCreate,
    TaskTypeOut,
    TaskTypeUpdate,
    TaskUpdate,
    ReminderRuleCreate,
    ReminderRuleOut,
    ReminderRuleUpdate,
    UserOut,
    ZaloIncomingRequest,
)
from .reminders import (
    create_reminder_rule,
    create_task_nudge_rule,
    diagnose_reminder_rules,
    is_reminder_internal_token_valid,
    reminder_internal_token_configured,
    run_reminder_rule_now,
    run_reminder_tick,
    update_reminder_rule,
)
from .services import (
    get_subtask_or_404,
    get_task_attachment_or_404,
    get_task_comment_or_404,
    get_task_or_404,
    list_tasks,
    next_list_order,
    seed_reference_data,
)
from .storage import StorageError, delete_object, is_storage_enabled, sign_object_url, upload_bytes
from .storage import ensure_bucket_exists
from .zalo_commands import handle_zalo_incoming
from .vikunja import (
    get_vikunja_client,
    handle_vikunja_webhook,
    migrate_tasks_to_vikunja,
    reconcile_vikunja_bridge,
    require_vikunja_or_503,
    sync_vikunja_users,
    vikunja_bridge_summary,
)

MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
MEMBER_ALLOWED_STATUSES = {TaskStatus.todo, TaskStatus.doing, TaskStatus.review}

settings = get_settings()
app = FastAPI(title=settings.app_name)
logger = getLogger(__name__)


class AdminNotificationTestRequest(BaseModel):
    channel: NotificationChannel = NotificationChannel.user
    target_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2000)
    context: dict[str, Any] = Field(default_factory=dict)


class AdminNotificationPromptUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=20000)


class AdminGmailZaloConfigUpdate(BaseModel):
    enabled: bool | None = None
    gmail_address: str | None = Field(default=None, max_length=255)
    gmail_app_password: str | None = Field(default=None, max_length=255)
    gmail_imap_host: str | None = Field(default=None, max_length=255)
    gmail_imap_port: int | None = Field(default=None, ge=1, le=65535)
    gmail_imap_mailbox: str | None = Field(default=None, max_length=255)
    gmail_search_since_days: int | None = Field(default=None, ge=1, le=90)
    gmail_sale_from_addresses: str | None = Field(default=None, max_length=1000)
    gmail_sale_subject: str | None = Field(default=None, max_length=255)
    gmail_message_from_addresses: str | None = Field(default=None, max_length=1000)
    gmail_poll_max_results: int | None = Field(default=None, ge=1, le=100)
    zalo_worker_url: str | None = Field(default=None, max_length=1000)
    zalo_worker_token: str | None = Field(default=None, max_length=1000)
    zalo_shared_secret: str | None = Field(default=None, max_length=1000)
    zalo_group_id: str | None = Field(default=None, max_length=128)


class AdminGmailZaloTestRequest(BaseModel):
    message: str = Field(default='Test Gmail/Zalo monitor từ Task Manager.', min_length=1, max_length=1000)


ADMIN_NOTIFICATION_EVENT_TYPES = [
    'task_assigned_on_create',
    'task_submitted_for_review',
    'task_approved_ready',
    'task_done_by_member',
    'task_updated',
    'task_deleted',
    'vikunja_task_assigned',
    'vikunja_task_review',
    'vikunja_task_ready',
    'vikunja_task_done',
    'vikunja_task_changed',
    'reminder_daily_group_digest',
    'reminder_daily_member_checkin',
    'reminder_daily_strategy',
    'reminder_task_nudge',
    'reminder_admin_escalation',
    'gmail_sale_new',
    'gmail_message_new',
    'gmail_daily_digest',
]

CORE_DAILY_RULE_SPECS: dict[str, dict[str, Any]] = {
    'daily_group_digest': {
        'name': '8AM group digest',
        'rule_type': 'daily_group_digest',
        'schedule_type': 'daily',
        'schedule_time': time(8, 0),
        'target_channel': NotificationChannel.group,
    },
    'daily_member_checkin': {
        'name': '9AM member check-in',
        'rule_type': 'daily_member_checkin',
        'schedule_type': 'daily',
        'schedule_time': time(9, 0),
    },
    'daily_strategy': {
        'name': '9AM daily strategy',
        'rule_type': 'daily_strategy',
        'schedule_type': 'daily',
        'schedule_time': time(9, 0),
    },
}

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)


def _safe_filename(name: str) -> str:
    cleaned = sub(r'[^A-Za-z0-9._-]+', '_', name).strip('._')
    return cleaned or 'attachment.bin'


def _build_storage_path(task_id: int, original_name: str) -> str:
    return f'tasks/{task_id}/{uuid4()}-{_safe_filename(original_name)}'


def _build_data_url(mime_type: str, content: bytes) -> str:
    return f'data:{mime_type};base64,{base64.b64encode(content).decode("ascii")}'


def _default_link_attachment_name(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    path_name = unquote(basename(parsed.path or '')).strip()
    if path_name:
        return _safe_filename(path_name)
    host_name = (parsed.netloc or '').strip()
    if host_name:
        return host_name
    return 'Link'


def _normalize_type_prefix(type_name: str) -> str:
    cleaned = ' '.join(type_name.split()).strip()
    return f'[{cleaned}]'


def _auto_prefix_title_for_type(title: str, task_type: TaskType | None) -> str:
    if not task_type or not title:
        return title
    prefix = _normalize_type_prefix(task_type.name)
    if title.lower().startswith(prefix.lower()):
        return title
    return f'{prefix} {title}'


def _resolve_attachment_url(attachment: TaskAttachment) -> str:
    if not attachment.storage_path or not is_storage_enabled():
        return attachment.data_url

    try:
        return sign_object_url(attachment.storage_path)
    except StorageError as exc:
        logger.warning(f'Failed to sign attachment URL for attachment {attachment.id}: {exc}')
        return attachment.data_url


def _attachment_out(attachment: TaskAttachment) -> TaskAttachmentOut:
    uploader = UserOut.model_validate(attachment.uploader) if attachment.uploader else None
    return TaskAttachmentOut(
        id=attachment.id,
        task_id=attachment.task_id,
        uploaded_by=attachment.uploaded_by,
        name=attachment.name,
        mime_type=attachment.mime_type,
        size_bytes=attachment.size_bytes,
        data_url=_resolve_attachment_url(attachment),
        is_image=attachment.is_image,
        created_at=attachment.created_at,
        uploader=uploader,
    )


def _create_link_attachment_record(
    db: Session,
    *,
    task_id: int,
    actor_id: str,
    url: str,
    name: str | None = None,
) -> TaskAttachment:
    clean_url = url.strip()
    attachment = TaskAttachment(
        task_id=task_id,
        uploaded_by=actor_id,
        name=(name or '').strip() or _default_link_attachment_name(clean_url),
        mime_type='text/uri-list',
        size_bytes=0,
        data_url=clean_url,
        storage_path=None,
        is_image=False,
    )
    db.add(attachment)
    return attachment


def _forbidden(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=message)


def _ensure_admin(actor: User) -> None:
    if not _is_admin(actor):
        raise _forbidden('Only admins can manage notification settings.')


def _admin_notifications_ui_html() -> str:
    return r"""
<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Hazel Noti Control</title>
  <style>
    :root {
      --bg: #171a21;
      --panel: #222733;
      --panel-2: #2b3140;
      --text: #f3f4f8;
      --muted: #aeb6c8;
      --line: #3a4252;
      --accent: #8fb7ff;
      --good: #8de6b2;
      --warn: #ffd36e;
      --bad: #ff9a9a;
      --radius: 18px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      background:
        radial-gradient(circle at 20% 0%, rgba(143,183,255,.22), transparent 34rem),
        radial-gradient(circle at 90% 10%, rgba(141,230,178,.12), transparent 26rem),
        var(--bg);
      font-family: ui-rounded, "Avenir Next", "SF Pro Rounded", "Nunito", system-ui, sans-serif;
    }
    main { width: min(1180px, calc(100% - 28px)); margin: 0 auto; padding: 28px 0 48px; }
    header { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 22px; }
    h1 { margin: 0; font-size: clamp(32px, 5vw, 58px); line-height: .92; letter-spacing: -.04em; }
    h2 { margin: 0 0 14px; font-size: 22px; }
    p { color: var(--muted); }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 16px; }
    .card { grid-column: span 6; background: rgba(34,39,51,.86); border: 1px solid var(--line); border-radius: var(--radius); padding: 18px; box-shadow: 0 20px 60px rgba(0,0,0,.22); }
    .wide { grid-column: span 12; }
    .metric-row { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
    .metric { background: var(--panel-2); border: 1px solid var(--line); border-radius: 14px; padding: 12px; }
    .metric b { display: block; font-size: 26px; }
    .metric span { color: var(--muted); font-size: 13px; }
    label { display: block; color: var(--muted); font-size: 13px; margin: 10px 0 6px; }
    input, select, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 11px 12px;
      color: var(--text);
      background: #1d222c;
      outline: none;
      font: inherit;
    }
    textarea { min-height: 92px; resize: vertical; }
    button {
      appearance: none;
      border: 0;
      border-radius: 999px;
      padding: 11px 16px;
      color: #12151c;
      background: var(--accent);
      font-weight: 800;
      cursor: pointer;
    }
    button.secondary { background: #394254; color: var(--text); }
    button.danger { background: #4b3035; color: var(--bad); }
    button.good { background: #234536; color: var(--good); }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 14px; }
    .pill { display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px; border-radius: 999px; background: #303747; color: var(--muted); font-size: 13px; }
    .ok { color: var(--good); } .warn { color: var(--warn); } .bad { color: var(--bad); }
    table { width: 100%; border-collapse: collapse; overflow: hidden; border-radius: 14px; }
    th, td { text-align: left; border-bottom: 1px solid var(--line); padding: 11px 8px; vertical-align: top; }
    th { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }
    td { font-size: 14px; }
    .mini { color: var(--muted); font-size: 12px; }
    .mono { font-family: "SF Mono", "JetBrains Mono", ui-monospace, monospace; font-size: 12px; word-break: break-all; }
    pre { white-space: pre-wrap; overflow: auto; background: #11151d; border: 1px solid var(--line); border-radius: 14px; padding: 12px; max-height: 260px; }
    .split { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .tabs { display: flex; gap: 10px; flex-wrap: wrap; margin: 0 0 18px; }
    .tab {
      color: var(--text);
      background: rgba(43,49,64,.72);
      border: 1px solid var(--line);
      box-shadow: inset 0 0 0 1px rgba(255,255,255,.02);
    }
    .tab.active { color: #12151c; background: var(--accent); }
    .hidden { display: none !important; }
    .dashboard-note {
      margin: 0 0 18px;
      padding: 12px 14px;
      border: 1px solid rgba(143,183,255,.28);
      border-radius: 16px;
      background: rgba(143,183,255,.08);
      color: var(--muted);
    }
    .event-card {
      border-left: 4px solid var(--accent);
      background: rgba(17,21,29,.48);
      border-radius: 14px;
      padding: 10px 12px;
      margin: 8px 0;
    }
    .toolbar { display: flex; justify-content: space-between; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
    @media (max-width: 760px) {
      header { display: block; }
      .card { grid-column: span 12; }
      .metric-row, .split { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <p class="pill">Hazel Bridge</p>
      <h1>Noti Control</h1>
      <p>Dashboard quản lý notification, reminder, prompt và cron theo giờ Việt Nam.</p>
    </div>
    <div class="pill" id="authState">Chưa đăng nhập</div>
  </header>

  <nav class="tabs">
    <button class="tab active" data-tab="overview" onclick="showTab('overview')">Tổng quan</button>
    <button class="tab" data-tab="rules" onclick="showTab('rules')">Reminder rules</button>
    <button class="tab" data-tab="test" onclick="showTab('test')">Test lab</button>
    <button class="tab" data-tab="prompts" onclick="showTab('prompts')">Prompts</button>
    <button class="tab" data-tab="logs" onclick="showTab('logs')">Logs</button>
  </nav>
  <p class="dashboard-note" id="dashboardNote">Mọi thời gian trên dashboard hiển thị theo Asia/Ho_Chi_Minh (GMT+7). Prompt Markdown sửa xong có hiệu lực ngay, không cần restart.</p>

  <section class="grid">
    <div class="card" id="loginCard" data-panel="overview">
      <h2>Đăng nhập</h2>
      <div class="split">
        <div><label>Username</label><input id="username" value="admin" autocomplete="username" /></div>
        <div><label>Password</label><input id="password" type="password" autocomplete="current-password" /></div>
      </div>
      <div class="actions">
        <button onclick="login()">Login</button>
        <button class="secondary" onclick="logout()">Logout</button>
      </div>
    </div>

    <div class="card" data-panel="overview">
      <h2>System</h2>
      <div class="metric-row" id="metrics"></div>
      <div class="actions">
        <button onclick="loadAll()">Refresh</button>
        <button class="good" onclick="runReconcile()">Run reconcile</button>
        <button class="secondary" onclick="runTick()">Run reminder tick</button>
        <button class="secondary" onclick="dispatchPending()">Dispatch pending</button>
      </div>
    </div>

    <div class="card" data-panel="overview">
      <h2>Skill Deck</h2>
      <p>Những thao tác anh sẽ dùng nhiều: chuẩn hóa bộ daily, test nhanh từng nhóm việc, và lấy plan cài scheduler chắc chắn hơn cron.</p>
      <div class="actions">
        <button onclick="bootstrapCoreRules()">Chuẩn hóa daily</button>
        <button class="secondary" onclick="bootstrapCoreRules(true)">Chuẩn hóa + tắt rule trùng</button>
        <button class="secondary" onclick="showSchedulerPlan()">Xem plan systemd timer</button>
      </div>
      <div class="actions">
        <button class="good" onclick="testCoreRule('daily_group_digest')">Test group digest</button>
        <button class="good" onclick="testCoreRule('daily_member_checkin')">Test member check-in</button>
        <button class="good" onclick="testCoreRule('daily_strategy')">Test daily strategy</button>
      </div>
      <div id="coreRules"></div>
    </div>

    <div class="card" data-panel="test">
      <h2>Test Zalo</h2>
      <div class="split">
        <div><label>Channel</label><select id="testChannel"><option value="user">user</option><option value="group">group</option></select></div>
        <div><label>Target ID</label><input id="testTarget" placeholder="zalo user id hoặc group id" /></div>
      </div>
      <label>Message</label>
      <textarea id="testMessage">Test noti từ Hazel Bridge nè.</textarea>
      <div class="actions"><button onclick="sendTest()">Send test</button></div>
    </div>

    <div class="card" data-panel="rules" id="ruleFormCard">
      <h2 id="ruleFormTitle">Tạo reminder nhanh</h2>
      <p id="ruleFormHint">Chọn rule bên dưới để edit, hoặc tạo rule mới tại đây.</p>
      <label>Name</label><input id="ruleName" placeholder="Daily group digest 08:00" />
      <div class="split">
        <div><label>Type</label><select id="ruleType">
          <option value="daily_group_digest">daily_group_digest</option>
          <option value="daily_member_checkin">daily_member_checkin</option>
          <option value="daily_strategy">daily_strategy</option>
          <option value="task_nudge">task_nudge</option>
        </select></div>
        <div><label>Schedule</label><select id="scheduleType"><option value="daily">daily</option><option value="interval">interval</option></select></div>
      </div>
      <div class="split">
        <div><label>Enabled</label><select id="ruleEnabled"><option value="true">enabled</option><option value="false">disabled</option></select></div>
        <div></div>
      </div>
      <div class="split">
        <div><label>HH:MM</label><input id="scheduleTime" placeholder="08:00" /></div>
        <div><label>Interval minutes</label><input id="intervalMinutes" type="number" min="1" placeholder="60" /></div>
      </div>
      <div class="split">
        <div><label>Target channel</label><select id="targetChannel"><option value="">auto</option><option value="user">user</option><option value="group">group</option></select></div>
        <div><label>Target ID</label><input id="targetId" placeholder="optional" /></div>
      </div>
      <div class="split">
        <div><label>User ID</label><input id="ruleUserId" placeholder="optional personal user id" /></div>
        <div><label>Task ID</label><input id="ruleTaskId" type="number" min="1" placeholder="optional task id" /></div>
      </div>
      <div class="split">
        <div><label>Max runs/day</label><input id="maxRunsPerDay" type="number" min="1" placeholder="optional" /></div>
        <div><label>Escalation after</label><input id="escalationAfter" type="number" min="1" placeholder="minutes or runs" /></div>
      </div>
      <input id="editingRuleId" type="hidden" />
      <div class="actions">
        <button id="ruleCreateButton" onclick="createRule()">Create rule</button>
        <button id="ruleSaveButton" class="good" onclick="saveRule()" style="display:none">Save rule</button>
        <button id="ruleCancelButton" class="secondary" onclick="clearRuleForm()" style="display:none">Cancel edit</button>
      </div>
    </div>

    <div class="card wide" data-panel="test">
      <h2>Contacts / Target IDs</h2>
      <p>Bấm vào ID để fill qua form test Zalo hoặc target reminder.</p>
      <div id="contacts"></div>
    </div>

    <div class="card wide" data-panel="rules">
      <h2>Reminder rules</h2>
      <div id="rules"></div>
    </div>

    <div class="card wide" data-panel="logs">
      <h2>Cron / Reminder runs</h2>
      <p>Nhìn nhanh cron app-level có tạo run/reconcile đều không. Cron hệ thống vẫn nằm ở VPS crontab, còn đây là dấu vết backend nhận được.</p>
      <div id="runs"></div>
    </div>

    <div class="card wide" data-panel="prompts">
      <h2>Notification prompts</h2>
      <p>Prompt global là nền chung. Prompt theo event sẽ được ghép thêm khi đúng loại thông báo, giúp mỗi noti có giọng riêng.</p>
      <div class="split">
        <div><label>Prompt scope</label><select id="promptScope" onchange="loadPrompt()"><option value="">Global notification writer</option></select></div>
        <div><label>File</label><input id="promptPath" readonly /></div>
      </div>
      <textarea id="notificationPrompt" style="min-height: 260px"></textarea>
      <div class="actions">
        <button onclick="savePrompt()">Save prompt</button>
        <button class="secondary" onclick="loadPrompt()">Reload prompt</button>
      </div>
    </div>

    <div class="card wide" data-panel="overview logs test rules prompts">
      <h2>Result</h2>
      <pre id="result">Sẵn sàng rồi anh.</pre>
    </div>
  </section>
</main>

<script>
const root = location.pathname.split('/admin/notifications')[0] || '';
const adminBase = root + '/admin/notifications';
let token = localStorage.getItem('hazel-noti-token') || '';
let activeTab = 'overview';
let promptEventTypes = [];

function showTab(tab) {
  activeTab = tab;
  document.querySelectorAll('.tab').forEach(button => button.classList.toggle('active', button.dataset.tab === tab));
  document.querySelectorAll('[data-panel]').forEach(panel => {
    const panels = String(panel.dataset.panel || '').split(/\s+/);
    panel.classList.toggle('hidden', !panels.includes(tab));
  });
}

function headers(extra = {}) {
  return token ? {'Authorization': 'Bearer ' + token, ...extra} : extra;
}
function show(data) {
  document.getElementById('result').textContent = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
}
function showError(error) {
  const message = error && error.message ? error.message : String(error);
  show(`Lỗi: ${message}`);
}
function setAuthState() {
  document.getElementById('authState').textContent = token ? 'Đã có token' : 'Chưa đăng nhập';
}
function formatDateTime(value, fallback = '') {
  if (!value) return fallback;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return fallback || value;
  return new Intl.DateTimeFormat('vi-VN', {
    timeZone: 'Asia/Ho_Chi_Minh',
    dateStyle: 'short',
    timeStyle: 'medium',
  }).format(date) + ' GMT+7';
}
function secondsLabel(seconds) {
  if (seconds === null || seconds === undefined) return 'chưa có';
  if (seconds < 60) return `${seconds}s trước`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} phút trước`;
  return `${Math.floor(seconds / 3600)} giờ trước`;
}
async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: headers(opts.headers || {}) });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = text; }
  if (!res.ok) throw new Error(typeof data === 'string' ? data : (data.detail || JSON.stringify(data)));
  return data;
}
async function runAction(label, fn) {
  try {
    show(`${label}...`);
    return await fn();
  } catch (error) {
    showError(error);
    throw error;
  }
}
async function login() {
  const data = await api(root + '/auth/login', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({username: username.value, password: password.value})
  });
  token = data.access_token;
  localStorage.setItem('hazel-noti-token', token);
  setAuthState();
  await loadAll();
}
function logout() {
  token = '';
  localStorage.removeItem('hazel-noti-token');
  setAuthState();
  show('Đã logout.');
}
function renderMetrics(status) {
  const c = status.notification_counts || {};
  const r = status.reminder_counts || {};
  const config = status.config || {};
  const cron = status.cron_health || {};
  const reconcileState = cron.vikunja_reconcile_running ? 'OK' : 'Cần kiểm tra';
  document.getElementById('metrics').innerHTML = [
    ['Pending', c.pending || 0, 'Đang chờ gửi'],
    ['Sent', c.sent || 0, 'Đã gửi'],
    ['Failed', c.failed || 0, 'Lỗi'],
    ['Reconcile', reconcileState, secondsLabel(cron.vikunja_reconcile_seconds_since)],
    ['Rules', r.enabled || 0, `${r.total || 0} rules tổng`],
  ].map(x => `<div class="metric"><b>${x[1]}</b><span>${x[0]} · ${x[2]}</span></div>`).join('');
  document.getElementById('dashboardNote').textContent =
    `Giờ server: ${cron.now_label || formatDateTime(cron.now)} · Reconcile cuối: ${cron.vikunja_reconcile_last_run_label || 'chưa có'} · Event cuối: ${cron.last_notification_event_label || 'chưa có'}`;
  if (config.zalo_group_id) document.getElementById('testTarget').placeholder = config.zalo_group_id;
}
function renderCoreRules(summary) {
  const entries = Object.entries(summary || {});
  if (!entries.length) {
    document.getElementById('coreRules').innerHTML = '<p class="mini">Chưa có core rule summary.</p>';
    return;
  }
  document.getElementById('coreRules').innerHTML = entries.map(([ruleType, item]) => `
    <div class="event-card">
      <b>${escapeHtml(item.label || ruleType)}</b>
      <div class="mini">rule #${item.canonical_rule_id || 'missing'} · ${item.schedule_time ? normalizeTime(item.schedule_time) + ' GMT+7' : 'chưa set giờ'} · ${item.enabled ? 'enabled' : 'disabled'} · duplicate: ${(item.duplicate_rule_ids || []).length}</div>
    </div>
  `).join('');
}
function useTarget(channel, targetId) {
  document.getElementById('testChannel').value = channel;
  document.getElementById('testTarget').value = targetId;
  document.getElementById('targetChannel').value = channel;
  document.getElementById('targetId').value = targetId;
  show(`Đã chọn ${channel}: ${targetId}`);
}
function renderContacts(status) {
  const contacts = status.contacts || {};
  const users = contacts.users || [];
  const groups = contacts.groups || [];
  const userRows = users.map(user => `<tr>
    <td>${escapeHtml(user.name || user.username || '')}<br><span class="mini">${escapeHtml(user.username || '')} · ${escapeHtml(user.role || '')}</span></td>
    <td><span class="mono">${escapeHtml(user.zalo_user_id || 'missing')}</span></td>
    <td><span class="mono">${escapeHtml(user.user_id || '')}</span></td>
    <td><span class="mono">${escapeHtml(user.vikunja_user_id || '')}</span></td>
    <td>${user.zalo_user_id ? `<button class="secondary" onclick="useTarget('user', '${escapeAttr(user.zalo_user_id)}')">Use</button>` : '<span class="bad">No Zalo</span>'}</td>
  </tr>`).join('');
  const groupRows = groups.map(group => `<tr>
    <td>${escapeHtml(group.name || 'Group')}<br><span class="mini">${escapeHtml(group.source || '')}</span></td>
    <td><span class="mono">${escapeHtml(group.group_id || '')}</span></td>
    <td colspan="2">${escapeHtml(group.note || '')}</td>
    <td><button class="secondary" onclick="useTarget('group', '${escapeAttr(group.group_id)}')">Use</button></td>
  </tr>`).join('');
  document.getElementById('contacts').innerHTML = `
    <table>
      <thead><tr><th>Name</th><th>Zalo / Group ID</th><th>User ID</th><th>Vikunja ID</th><th></th></tr></thead>
      <tbody>${groupRows}${userRows}</tbody>
    </table>`;
}
function renderRules(rules, diagnostics = []) {
  window.reminderRulesById = Object.fromEntries(rules.map(rule => [String(rule.id), rule]));
  window.reminderRulesByType = Object.fromEntries(rules.map(rule => [String(rule.rule_type), rule]));
  const diagnosticsById = Object.fromEntries((diagnostics || []).map(item => [String(item.id), item]));
  window.reminderRuleDiagnosticsById = diagnosticsById;
  if (!rules.length) {
    document.getElementById('rules').innerHTML = '<p>Chưa có reminder rule.</p>';
    return;
  }
  document.getElementById('rules').innerHTML = `<div class="toolbar"><span class="pill">${rules.filter(rule => rule.enabled).length} enabled / ${rules.length} total</span><button class="secondary" onclick="clearRuleForm(); showTab('rules')">New rule</button></div><table><thead><tr><th>ID</th><th>Name</th><th>Type</th><th>Schedule</th><th>Target</th><th>Status</th><th></th></tr></thead><tbody>${
    rules.map(rule => {
      const diag = diagnosticsById[String(rule.id)] || {};
      const targetText = (diag.targets || []).map(target => `${target.channel}:${target.user_name || target.target_id || 'missing'}`).join(', ');
      const healthClass = !rule.enabled ? 'bad' : diag.due_now ? 'warn' : (diag.target_count === 0 ? 'bad' : 'ok');
      return `<tr>
      <td>#${rule.id}</td>
      <td>${escapeHtml(rule.name)}<br><span class="mini">${escapeHtml(diag.note || '')}</span></td>
      <td>${ruleTypeLabel(rule.rule_type)}<br><span class="mini">${rule.rule_type}</span></td>
      <td>${rule.schedule_type}${rule.schedule_time ? ' · ' + normalizeTime(rule.schedule_time) + ' GMT+7' : ''}${rule.interval_minutes ? ' · mỗi ' + rule.interval_minutes + 'm' : ''}</td>
      <td>${rule.target_channel || 'auto'}<br><span class="pill">${escapeHtml(targetText || rule.target_id || rule.user_id || rule.task_id || 'auto')}</span></td>
      <td><span class="${rule.enabled ? 'ok' : 'bad'}">${rule.enabled ? 'enabled' : 'disabled'}</span><br><span class="${healthClass}">${diag.due_now ? 'due now' : `${diag.target_count ?? 0} targets`}</span><br><span class="mini">${escapeHtml(diag.last_run_at ? 'last ' + formatDateTime(diag.last_run_at) : 'chưa có run')}</span></td>
      <td>
        <button class="secondary" onclick="editRuleById(${rule.id})">Edit</button>
        <button class="good" onclick="testRule(${rule.id})">Test now</button>
        <button class="secondary" onclick="inspectRule(${rule.id})">Inspect</button>
        <button class="secondary" onclick="toggleRule(${rule.id}, ${!rule.enabled})">${rule.enabled ? 'Disable' : 'Enable'}</button>
      </td>
    </tr>`;
    }).join('')
  }</tbody></table>`;
}
function ruleTypeLabel(type) {
  return ({
    daily_group_digest: 'Tổng quan group',
    daily_member_checkin: 'Check-in từng người',
    daily_strategy: 'Gợi ý chiến lược',
    task_nudge: 'Nhắc task lặp lại',
  })[type] || type;
}
function renderRuns(status) {
  const events = status.latest_events || [];
  const runs = status.latest_reminder_runs || [];
  const interactions = status.latest_reminder_interactions || [];
  const eventRows = events.map(event => `<tr>
    <td>#${event.id}<br><span class="mini">${escapeHtml(event.event_type || '')}</span></td>
    <td>${escapeHtml(event.channel || '')}<br><span class="mono">${escapeHtml(event.target_id || '')}</span></td>
    <td><span class="${event.status === 'sent' ? 'ok' : event.status === 'failed' ? 'bad' : 'warn'}">${escapeHtml(event.status || '')}</span><br><span class="mini">${event.attempt_count || 0} attempts</span></td>
    <td>${escapeHtml(event.message || '')}<br><span class="mini">${escapeHtml(event.last_error || '')}</span></td>
    <td>${escapeHtml(event.created_at_label || formatDateTime(event.created_at))}</td>
  </tr>`).join('');
  const runRows = runs.map(run => `<tr>
    <td>#${run.id}<br><span class="mini">rule #${run.rule_id}</span></td>
    <td>${escapeHtml(run.rule_name || '')}<br><span class="mini">${escapeHtml(run.rule_type || '')}</span></td>
    <td>${escapeHtml(run.status || '')}</td>
    <td><span class="mono">${escapeHtml(run.scheduled_for_label || formatDateTime(run.scheduled_for))}</span><br><span class="mini">created ${escapeHtml(run.created_at_label || formatDateTime(run.created_at))}</span></td>
    <td>${run.notification_event_id ? '#' + run.notification_event_id : ''}</td>
  </tr>`).join('');
  const interactionRows = interactions.map(item => `<tr>
    <td>#${item.id}<br><span class="mini">run #${item.run_id || ''}</span></td>
    <td>${escapeHtml(item.interaction_type || '')}</td>
    <td>${escapeHtml(item.text || '')}</td>
    <td><span class="mono">${escapeHtml(item.conversation_id || '')}</span></td>
    <td>${escapeHtml(item.created_at_label || formatDateTime(item.created_at))}</td>
  </tr>`).join('');
  document.getElementById('runs').innerHTML = `
    <h3>Latest notification events</h3>
    <table><thead><tr><th>ID</th><th>Target</th><th>Status</th><th>Message/Error</th><th>Created</th></tr></thead><tbody>${eventRows || '<tr><td colspan="5">Chưa có event.</td></tr>'}</tbody></table>
    <h3>Latest runs</h3>
    <table><thead><tr><th>ID</th><th>Rule</th><th>Status</th><th>Time</th><th>Event</th></tr></thead><tbody>${runRows || '<tr><td colspan="5">Chưa có run.</td></tr>'}</tbody></table>
    <h3>Latest interactions</h3>
    <table><thead><tr><th>ID</th><th>Type</th><th>Text</th><th>Conversation</th><th>Created</th></tr></thead><tbody>${interactionRows || '<tr><td colspan="5">Chưa có interaction.</td></tr>'}</tbody></table>`;
}
function renderPromptScopes(types) {
  promptEventTypes = types || [];
  const current = document.getElementById('promptScope').value;
  document.getElementById('promptScope').innerHTML =
    '<option value="">Global notification writer</option>' +
    promptEventTypes.map(type => `<option value="${escapeAttr(type)}">${escapeHtml(ruleTypeLabel(type) === type ? type : `${ruleTypeLabel(type)} · ${type}`)}</option>`).join('');
  document.getElementById('promptScope').value = current;
}
function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function escapeAttr(value) {
  return String(value).replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, ' ');
}
async function loadAll() {
  setAuthState();
  const [status, rules, promptData] = await Promise.all([
    api(adminBase + '/status'),
    api(root + '/reminders'),
    api(adminBase + '/prompt')
  ]);
  renderMetrics(status);
  renderCoreRules(status.core_rule_summary || {});
  renderContacts(status);
  renderRules(rules, status.rule_diagnostics || []);
  renderRuns(status);
  renderPromptScopes(status.event_prompt_types || []);
  document.getElementById('notificationPrompt').value = promptData.content || '';
  document.getElementById('promptPath').value = promptData.path || '';
  show(status);
}
async function sendTest() {
  const data = await runAction('Đang gửi test Zalo', () => api(adminBase + '/test', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({channel: testChannel.value, target_id: testTarget.value, message: testMessage.value})
  }));
  show(data);
}
async function runReconcile() {
  show(await runAction('Đang chạy reconcile', () => api(adminBase + '/reconcile', {method: 'POST'})));
}
async function dispatchPending() {
  show(await runAction('Đang dispatch pending notifications', () => api(adminBase + '/dispatch', {method: 'POST'})));
}
async function runTick() {
  show(await runAction('Đang chạy reminder tick', () => api(root + '/reminders/tick', {method: 'POST'})));
}
async function bootstrapCoreRules(cleanupDuplicates = false) {
  const suffix = cleanupDuplicates ? '?cleanup_duplicates=true' : '';
  const data = await runAction(
    cleanupDuplicates ? 'Đang chuẩn hóa daily rules và tắt rule trùng' : 'Đang chuẩn hóa daily rules',
    () => api(adminBase + '/bootstrap-core-rules' + suffix, {method: 'POST'})
  );
  show(data);
  await loadAll();
}
async function showSchedulerPlan() {
  const data = await runAction('Đang lấy plan systemd timer', () => api(adminBase + '/scheduler/install-plan'));
  show(data);
}
async function testCoreRule(ruleType) {
  const rule = (window.reminderRulesByType || {})[String(ruleType)];
  if (!rule) {
    show(`Chưa có rule loại ${ruleType}. Bấm "Chuẩn hóa daily" trước nha.`);
    return;
  }
  await testRule(rule.id);
}
async function toggleRule(id, enabled) {
  show(await runAction(`Đang ${enabled ? 'enable' : 'disable'} rule #${id}`, () => api(root + '/reminders/' + id, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({enabled})
  })));
  await loadAll();
}
function clearRuleForm() {
  editingRuleId.value = '';
  document.getElementById('ruleFormTitle').textContent = 'Tạo reminder nhanh';
  document.getElementById('ruleFormHint').textContent = 'Chọn rule bên dưới để edit, hoặc tạo rule mới tại đây.';
  ruleName.value = '';
  ruleType.disabled = false;
  ruleType.value = 'daily_group_digest';
  ruleEnabled.value = 'true';
  scheduleType.value = 'daily';
  scheduleTime.value = '';
  intervalMinutes.value = '';
  targetChannel.value = '';
  targetId.value = '';
  ruleUserId.value = '';
  ruleTaskId.value = '';
  maxRunsPerDay.value = '';
  escalationAfter.value = '';
  document.getElementById('ruleCreateButton').style.display = '';
  document.getElementById('ruleSaveButton').style.display = 'none';
  document.getElementById('ruleCancelButton').style.display = 'none';
}
function editRule(rule) {
  showTab('rules');
  editingRuleId.value = rule.id;
  document.getElementById('ruleFormTitle').textContent = `Đang edit reminder #${rule.id}`;
  document.getElementById('ruleFormHint').textContent = 'Form đã được fill từ rule hiện tại. Chỉnh xong bấm Save rule để lưu.';
  ruleName.value = rule.name || '';
  ruleType.value = rule.rule_type || 'daily_group_digest';
  ruleType.disabled = true;
  ruleEnabled.value = rule.enabled ? 'true' : 'false';
  scheduleType.value = rule.schedule_type || 'daily';
  scheduleTime.value = normalizeTime(rule.schedule_time || '');
  intervalMinutes.value = rule.interval_minutes || '';
  targetChannel.value = rule.target_channel || '';
  targetId.value = rule.target_id || '';
  ruleUserId.value = rule.user_id || '';
  ruleTaskId.value = rule.task_id || '';
  maxRunsPerDay.value = rule.max_runs_per_day || '';
  escalationAfter.value = rule.escalation_after_minutes || rule.escalation_after_runs || '';
  document.getElementById('ruleCreateButton').style.display = 'none';
  document.getElementById('ruleSaveButton').style.display = '';
  document.getElementById('ruleCancelButton').style.display = '';
  setTimeout(() => document.getElementById('ruleFormCard').scrollIntoView({behavior: 'smooth', block: 'start'}), 50);
  show(`Đã mở form edit reminder #${rule.id}. Chỉnh field phía trên rồi bấm "Save rule" để lưu.`);
}
function editRuleById(id) {
  const rule = (window.reminderRulesById || {})[String(id)];
  if (!rule) return show(`Không tìm thấy rule #${id} trên UI hiện tại.`);
  editRule(rule);
}
function inspectRule(id) {
  const rule = (window.reminderRulesById || {})[String(id)];
  const diagnostics = (window.reminderRuleDiagnosticsById || {})[String(id)];
  show({rule, diagnostics});
}
function normalizeTime(value) {
  return String(value || '').slice(0, 5);
}
function buildRulePayload({forUpdate = false} = {}) {
  const payload = {
    name: ruleName.value,
    enabled: ruleEnabled.value !== 'false',
    schedule_type: scheduleType.value,
    timezone: 'Asia/Ho_Chi_Minh',
    target_channel: targetChannel.value || null,
    target_id: targetId.value || null,
    user_id: ruleUserId.value || null,
    task_id: ruleTaskId.value ? Number(ruleTaskId.value) : null,
    interval_minutes: intervalMinutes.value ? Number(intervalMinutes.value) : null,
    max_runs_per_day: maxRunsPerDay.value ? Number(maxRunsPerDay.value) : null,
    payload: {}
  };
  if (!forUpdate) payload.rule_type = ruleType.value;
  if (scheduleTime.value) payload.schedule_time = scheduleTime.value;
  const escalation = escalationAfter.value ? Number(escalationAfter.value) : null;
  if (escalation) {
    if ((forUpdate ? ruleType.value : payload.rule_type) === 'task_nudge') payload.escalation_after_runs = escalation;
    else payload.escalation_after_minutes = escalation;
  }
  return payload;
}
async function createRule() {
  const payload = buildRulePayload();
  show(await runAction('Đang tạo reminder rule', () => api(root + '/reminders', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  })));
  clearRuleForm();
  await loadAll();
}
async function saveRule() {
  if (!editingRuleId.value) return show('Chưa chọn rule để edit.');
  const payload = buildRulePayload({forUpdate: true});
  show(await runAction(`Đang lưu reminder #${editingRuleId.value}`, () => api(root + '/reminders/' + editingRuleId.value, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  })));
  clearRuleForm();
  await loadAll();
}
async function testRule(id) {
  const data = await runAction(`Đang test rule #${id}`, () => api(adminBase + '/reminders/' + id + '/test', {method: 'POST'}));
  const summary = {
    rule_id: data.rule_id,
    rule_name: data.rule_name,
    targets_checked: data.targets_checked,
    runs_created: data.runs_created,
    runs_deduped: data.runs_deduped,
    note: data.note || null,
    dispatch: data.dispatch,
    targets: data.targets || [],
  };
  show(summary);
  await loadAll();
}
async function loadPrompt() {
  const eventType = document.getElementById('promptScope').value;
  const suffix = eventType ? '?event_type=' + encodeURIComponent(eventType) : '';
  const data = await api(adminBase + '/prompt' + suffix);
  document.getElementById('notificationPrompt').value = data.content || '';
  document.getElementById('promptPath').value = data.path || '';
  show(data);
}
async function savePrompt() {
  const eventType = document.getElementById('promptScope').value;
  const suffix = eventType ? '?event_type=' + encodeURIComponent(eventType) : '';
  const data = await runAction('Đang lưu prompt', () => api(adminBase + '/prompt' + suffix, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({content: document.getElementById('notificationPrompt').value})
  }));
  document.getElementById('promptPath').value = data.path || '';
  show(data);
}
setAuthState();
showTab(activeTab);
if (token) loadAll().catch(err => show(err.message));
</script>
</body>
</html>
"""


def _trigger_task_created_notification(db: Session, task: Task) -> None:
    try:
        enqueue_task_created_notifications(db, task)
    except Exception as exc:  # pragma: no cover - defensive guard for notification side effects
        db.rollback()
        logger.warning('Failed to enqueue/deliver task-created notification for task_id=%s: %s', task.id, exc)


def _trigger_status_transition_notifications(
    db: Session,
    *,
    task: Task,
    previous_status: TaskStatus,
    actor: User,
) -> None:
    try:
        enqueue_task_status_transition_notifications(
            db,
            task=task,
            previous_status=previous_status,
            actor=actor,
        )
    except Exception as exc:  # pragma: no cover - defensive guard for notification side effects
        db.rollback()
        logger.warning(
            'Failed to enqueue/deliver task-status notifications for task_id=%s (%s -> %s): %s',
            task.id,
            previous_status.value,
            task.status.value,
            exc,
        )


def _trigger_task_updated_notification(db: Session, *, task: Task, actor: User, changed_fields: list[str]) -> None:
    try:
        enqueue_task_updated_notifications(db, task=task, actor=actor, changed_fields=changed_fields)
    except Exception as exc:  # pragma: no cover - defensive guard for notification side effects
        db.rollback()
        logger.warning('Failed to enqueue/deliver task-updated notification for task_id=%s: %s', task.id, exc)


def _trigger_task_deleted_notification(db: Session, *, task: Task, actor: User) -> None:
    try:
        enqueue_task_deleted_notifications(db, task=task, actor=actor)
    except Exception as exc:  # pragma: no cover - defensive guard for notification side effects
        db.rollback()
        logger.warning('Failed to enqueue/deliver task-deleted notification for task_id=%s: %s', task.id, exc)


def require_internal_token(
    x_internal_token: str | None = Header(default=None, alias='X-Internal-Token'),
) -> None:
    if not settings.notify_internal_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Internal notification token is not configured.',
        )
    if not x_internal_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Missing X-Internal-Token.')
    if not is_internal_token_valid(x_internal_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Invalid internal token.')


def require_reminder_internal_token(
    x_internal_token: str | None = Header(default=None, alias='X-Internal-Token'),
) -> None:
    if not reminder_internal_token_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Internal reminder token is not configured.',
        )
    if not x_internal_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Missing X-Internal-Token.')
    if not is_reminder_internal_token_valid(x_internal_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Invalid internal token.')


def get_actor(
    db: Session = Depends(get_db),
    actor_id: str | None = Header(default=None, alias='X-Actor-Id'),
    authorization: str | None = Header(default=None, alias='Authorization'),
) -> User:
    resolved_actor_id: str | None = None

    try:
        bearer_token = extract_bearer_token(authorization)
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    if bearer_token:
        try:
            payload = decode_access_token(bearer_token, secret_key=settings.auth_secret_key)
            resolved_actor_id = str(payload['sub'])
        except AuthError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    elif actor_id:
        resolved_actor_id = actor_id

    if not resolved_actor_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Authentication required. Use Bearer token or X-Actor-Id.',
        )

    actor = db.get(User, resolved_actor_id)
    if not actor:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid authentication user.')
    if not actor.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='User is inactive.')
    return actor


def _is_admin(actor: User) -> bool:
    return (actor.role or '').lower() == 'admin'


def _ensure_task_access(task: Task, actor: User) -> None:
    if _is_admin(actor):
        return
    if task.assigned_to != actor.id:
        raise _forbidden('Members can only access their own tasks.')


def _validate_member_status(next_status: TaskStatus, current_status: TaskStatus | None = None) -> None:
    if next_status in MEMBER_ALLOWED_STATUSES:
        return
    if current_status == TaskStatus.ready and next_status == TaskStatus.done:
        return
    raise _forbidden('Members can only move tasks up to review, or mark ready tasks as done.')


def _validate_status_transition(task: Task, next_status: TaskStatus, actor: User) -> None:
    if next_status == task.status:
        return

    if _is_admin(actor):
        if next_status == TaskStatus.ready and task.status != TaskStatus.review:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Only tasks in review can be approved to ready.',
            )
        return

    _validate_member_status(next_status, task.status)


def _can_convert_task(task: Task, actor: User) -> None:
    if _is_admin(actor):
        return
    if task.assigned_to != actor.id:
        raise _forbidden('Only admins or the assignee can convert this task.')


def _apply_role_on_create(values: dict, actor: User) -> dict:
    normalized = dict(values)
    if _is_admin(actor):
        if normalized.get('status') == TaskStatus.ready:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='New tasks must be reviewed before ready.',
            )
        return normalized

    assigned_to = normalized.get('assigned_to')
    if assigned_to and assigned_to != actor.id:
        raise _forbidden('Members cannot assign tasks to others.')

    normalized['assigned_to'] = actor.id
    normalized['created_by'] = actor.id
    if normalized.get('status') is not None:
        _validate_member_status(normalized['status'], None)
    return normalized


def _apply_role_on_update(task: Task, update_values: dict, actor: User) -> dict:
    normalized = dict(update_values)
    if _is_admin(actor):
        if (
            'status' in normalized
            and normalized['status'] == TaskStatus.ready
            and task.status != TaskStatus.review
            and normalized['status'] != task.status
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Only tasks in review can be approved to ready.',
            )
        return normalized

    _ensure_task_access(task, actor)

    if 'assigned_to' in normalized and normalized['assigned_to'] != task.assigned_to:
        raise _forbidden('Members cannot reassign tasks.')
    normalized.pop('assigned_to', None)

    if 'created_by' in normalized:
        normalized['created_by'] = actor.id

    if 'status' in normalized and normalized['status'] != task.status:
        _validate_status_transition(task, normalized['status'], actor)

    return normalized


def _validate_task_update_references(db: Session, update_values: dict) -> None:
    assigned_to = update_values.get('assigned_to')
    if assigned_to is not None and not db.get(User, assigned_to):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Assignee not found.')

    created_by = update_values.get('created_by')
    if created_by is not None and not db.get(User, created_by):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Creator not found.')

    shop_id = update_values.get('shop_id')
    if shop_id is not None and not db.get(Shop, shop_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Shop not found.')

    type_id = update_values.get('type_id')
    if type_id is not None and not db.get(TaskType, type_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task type not found.')


@app.on_event('startup')
def on_startup() -> None:
    try:
        schema_name = settings.normalized_db_schema

        with engine.begin() as connection:
            if schema_name:
                connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"'))

        inspector = inspect(engine)
        required_tables = [
            'users',
            'shops',
            'task_types',
            'tasks',
            'subtasks',
            'task_comments',
            'task_attachments',
            'notification_events',
            'notification_deliveries',
            'zalo_incoming_commands',
            'bot_conversation_messages',
            'bot_conversation_states',
            'bot_memory_facts',
            'reminder_rules',
            'reminder_runs',
            'reminder_interactions',
            'vikunja_user_mappings',
            'vikunja_task_mappings',
            'vikunja_bridge_state',
            'gmail_monitor_events',
            'integration_configs',
        ]
        if any(not inspector.has_table(table, schema=schema_name) for table in required_tables):
            # Safety fallback for local/dev environments where migrations are not yet aligned.
            Base.metadata.create_all(
                bind=engine,
                tables=[
                    User.__table__,
                    Shop.__table__,
                    TaskType.__table__,
                    Task.__table__,
                    Subtask.__table__,
                    TaskComment.__table__,
                    TaskAttachment.__table__,
                    NotificationEvent.__table__,
                    NotificationDelivery.__table__,
                    ZaloIncomingCommand.__table__,
                    BotConversationMessage.__table__,
                    BotConversationState.__table__,
                    BotMemoryFact.__table__,
                    ReminderRule.__table__,
                    ReminderRun.__table__,
                    ReminderInteraction.__table__,
                    VikunjaUserMapping.__table__,
                    VikunjaTaskMapping.__table__,
                    VikunjaBridgeState.__table__,
                    GmailMonitorEvent.__table__,
                    IntegrationConfig.__table__,
                ],
            )

        # Compatibility shim for legacy deployments:
        # remove old FK constraints that still point task-related user columns
        # to local `<schema>.users` table (we now use social.users as canonical source).
        user_fk_targets = {
            'tasks': {'assigned_to', 'created_by'},
            'task_comments': {'author_id'},
            'task_attachments': {'uploaded_by'},
        }
        for table_name, columns in user_fk_targets.items():
            if not inspector.has_table(table_name, schema=schema_name):
                continue
            for fk in inspector.get_foreign_keys(table_name, schema=schema_name):
                constraint_name = fk.get('name')
                referred_table = fk.get('referred_table')
                constrained_columns = set(fk.get('constrained_columns') or [])
                if not constraint_name:
                    continue
                if referred_table != 'users':
                    continue
                if not (constrained_columns & columns):
                    continue

                qualified_table = f'"{schema_name}"."{table_name}"' if schema_name else f'"{table_name}"'
                with engine.begin() as connection:
                    connection.execute(
                        text(f'ALTER TABLE {qualified_table} DROP CONSTRAINT IF EXISTS "{constraint_name}"')
                    )

        # Compatibility shim for pre-storage deployments: add missing `storage_path` column if needed.
        if inspector.has_table('task_attachments', schema=schema_name):
            column_names = {column['name'] for column in inspector.get_columns('task_attachments', schema=schema_name)}
            if 'storage_path' not in column_names:
                target = f'"{schema_name}"."task_attachments"' if schema_name else '"task_attachments"'
                with engine.begin() as connection:
                    connection.execute(text(f'ALTER TABLE {target} ADD COLUMN storage_path VARCHAR(512)'))

        # Compatibility shim for conversion lineage: add missing `parent_task_id` on tasks if needed.
        if inspector.has_table('tasks', schema=schema_name):
            task_column_names = {column['name'] for column in inspector.get_columns('tasks', schema=schema_name)}
            if 'parent_task_id' not in task_column_names:
                target = f'"{schema_name}"."tasks"' if schema_name else '"tasks"'
                with engine.begin() as connection:
                    connection.execute(text(f'ALTER TABLE {target} ADD COLUMN parent_task_id INTEGER NULL'))

        # Compatibility shim: old notification_events may have user_id as UUID
        # while we now map social user IDs as VARCHAR.
        if inspector.has_table('notification_events', schema=schema_name):
            notif_columns = {
                column['name']: column for column in inspector.get_columns('notification_events', schema=schema_name)
            }
            user_id_column = notif_columns.get('user_id')
            if user_id_column and 'uuid' in str(user_id_column['type']).lower():
                target = f'"{schema_name}"."notification_events"' if schema_name else '"notification_events"'
                with engine.begin() as connection:
                    connection.execute(
                        text(f'ALTER TABLE {target} ALTER COLUMN user_id TYPE VARCHAR(64) USING user_id::text')
                    )

        with Session(engine) as db:
            seed_reference_data(db)

        if is_storage_enabled():
            ensure_bucket_exists()
        ensure_bot_files()
    except SQLAlchemyError as exc:
        logger.warning(f'Database is not ready for seed data. Original error: {exc}')
    except StorageError as exc:
        logger.warning(f'Supabase Storage is not ready: {exc}')


@app.get('/health')
def healthcheck() -> dict[str, str]:
    return {'status': 'ok'}


@app.get('/admin/notifications/ui', response_class=HTMLResponse)
def admin_notifications_ui() -> HTMLResponse:
    return HTMLResponse(_admin_notifications_ui_html())


def _parse_admin_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ZoneInfo(settings.app_timezone))
    return parsed


def _seconds_since(value: datetime | None, *, now: datetime) -> int | None:
    if value is None:
        return None
    return max(0, int((now - value.astimezone(now.tzinfo)).total_seconds()))


def _admin_datetime_label(value: datetime | None) -> str | None:
    if value is None:
        return None
    timezone = ZoneInfo(settings.app_timezone)
    localized = value.astimezone(timezone) if value.tzinfo else value.replace(tzinfo=timezone)
    suffix = 'GMT+7' if settings.app_timezone == 'Asia/Ho_Chi_Minh' else settings.app_timezone
    return f'{localized:%d/%m/%Y %H:%M:%S} {suffix}'


def _admin_contact_snapshot(db: Session) -> dict[str, Any]:
    mappings_by_user_id = {
        mapping.social_user_id: mapping
        for mapping in db.scalars(select(VikunjaUserMapping)).all()
    }
    users = db.scalars(
        select(User)
        .where(User.is_active.is_(True))
        .order_by(func.lower(func.coalesce(User.full_name, User.username)).asc(), func.lower(User.username).asc())
    ).all()

    group_ids: list[tuple[str, str]] = []
    if settings.zalo_group_id:
        group_ids.append(('ZALO_GROUP_ID', settings.zalo_group_id))
    for group_id in settings.zalo_allowed_group_id_list:
        group_ids.append(('ZALO_ALLOWED_GROUP_IDS', group_id))

    seen_groups: set[str] = set()
    groups: list[dict[str, str]] = []
    for source, group_id in group_ids:
        if not group_id or group_id in seen_groups:
            continue
        seen_groups.add(group_id)
        groups.append(
            {
                'name': 'Hazel group' if group_id == settings.zalo_group_id else 'Allowed group',
                'group_id': group_id,
                'source': source,
                'note': 'Dùng channel=group khi test gửi vào nhóm.',
            }
        )

    return {
        'users': [
            {
                'user_id': user.id,
                'username': user.username,
                'name': user.name,
                'role': user.role,
                'zalo_user_id': user.zalo_user_id,
                'vikunja_user_id': (
                    mappings_by_user_id[user.id].vikunja_user_id
                    if user.id in mappings_by_user_id
                    else None
                ),
            }
            for user in users
        ],
        'groups': groups,
    }


def _core_daily_rule_candidates(db: Session) -> list[ReminderRule]:
    return db.scalars(
        select(ReminderRule)
        .where(ReminderRule.task_id.is_(None))
        .where(ReminderRule.rule_type.in_(list(CORE_DAILY_RULE_SPECS.keys())))
        .order_by(ReminderRule.id.asc())
    ).all()


def _core_daily_rule_summary(db: Session) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    rules = _core_daily_rule_candidates(db)
    for rule_type, spec in CORE_DAILY_RULE_SPECS.items():
        matching = [rule for rule in rules if rule.rule_type.value == rule_type]
        canonical = matching[0] if matching else None
        summary[rule_type] = {
            'label': spec['name'],
            'canonical_rule_id': canonical.id if canonical else None,
            'enabled': canonical.enabled if canonical else False,
            'schedule_time': canonical.schedule_time.isoformat() if canonical and canonical.schedule_time else None,
            'duplicate_rule_ids': [rule.id for rule in matching[1:]],
            'count': len(matching),
        }
    return summary


def _bootstrap_core_daily_rules(db: Session, *, actor: User, cleanup_duplicates: bool = False) -> dict[str, Any]:
    existing = _core_daily_rule_candidates(db)
    created_ids: list[int] = []
    updated_ids: list[int] = []
    disabled_duplicate_ids: list[int] = []

    for rule_type, spec in CORE_DAILY_RULE_SPECS.items():
        matching = [rule for rule in existing if rule.rule_type.value == rule_type]
        canonical = matching[0] if matching else None
        values = {
            'name': spec['name'],
            'enabled': True,
            'schedule_type': spec['schedule_type'],
            'schedule_time': spec['schedule_time'],
            'timezone': settings.reminder_timezone,
            'target_channel': spec.get('target_channel'),
            'target_id': settings.zalo_group_id if rule_type == 'daily_group_digest' else None,
            'user_id': actor.id if rule_type == 'daily_strategy' else None,
            'task_id': None,
            'interval_minutes': None,
            'max_runs_per_day': None,
            'payload': {},
        }
        if canonical is None:
            created = create_reminder_rule(
                db,
                actor=actor,
                values={'rule_type': rule_type, **values},
            )
            created_ids.append(created.id)
            existing.append(created)
            matching = [created]
        else:
            update_reminder_rule(db, rule=canonical, values=values)
            updated_ids.append(canonical.id)

        if cleanup_duplicates and len(matching) > 1:
            for duplicate in matching[1:]:
                if duplicate.enabled:
                    duplicate.enabled = False
                    db.add(duplicate)
                    disabled_duplicate_ids.append(duplicate.id)
            db.commit()

    return {
        'created_rule_ids': created_ids,
        'updated_rule_ids': updated_ids,
        'disabled_duplicate_rule_ids': disabled_duplicate_ids,
        'core_rule_summary': _core_daily_rule_summary(db),
    }


def _scheduler_install_plan() -> dict[str, Any]:
    app_dir = '/opt/task-manager'
    service_name = 'taskmanager-reminder-tick.service'
    timer_name = 'taskmanager-reminder-tick.timer'
    service_path = f'{app_dir}/deploy/systemd/{service_name}'
    timer_path = f'{app_dir}/deploy/systemd/{timer_name}'
    runner_path = f'{app_dir}/deploy/systemd/run-reminder-tick.sh'
    installer_path = f'{app_dir}/deploy/systemd/install-reminder-timer.sh'
    readme_path = f'{app_dir}/deploy/systemd/README.md'
    install_commands = [
        f'cd {app_dir}',
        f'bash {installer_path}',
    ]
    debug_commands = [
        f'{runner_path}',
        f'systemctl status {timer_name} --no-pager',
        f'systemctl list-timers --all | grep {timer_name.replace(".timer", "")}',
        f'journalctl -u {service_name} -n 50 --no-pager',
    ]
    return {
        'app_dir': app_dir,
        'service_name': service_name,
        'timer_name': timer_name,
        'service_path': service_path,
        'timer_path': timer_path,
        'runner_path': runner_path,
        'installer_path': installer_path,
        'readme_path': readme_path,
        'install_commands': install_commands,
        'debug_commands': debug_commands,
    }

    seen_groups: set[str] = set()
    groups: list[dict[str, str]] = []
    for source, group_id in group_ids:
        if not group_id or group_id in seen_groups:
            continue
        seen_groups.add(group_id)
        groups.append(
            {
                'name': 'Hazel group' if group_id == settings.zalo_group_id else 'Allowed group',
                'group_id': group_id,
                'source': source,
                'note': 'Dùng channel=group khi test gửi vào nhóm.',
            }
        )

    return {
        'users': [
            {
                'user_id': user.id,
                'username': user.username,
                'name': user.name,
                'role': user.role,
                'zalo_user_id': user.zalo_user_id,
                'vikunja_user_id': (
                    mappings_by_user_id[user.id].vikunja_user_id
                    if user.id in mappings_by_user_id
                    else None
                ),
            }
            for user in users
        ],
        'groups': groups,
    }


def _safe_notification_event_type(event_type: str | None) -> str | None:
    if not event_type:
        return None
    return sub(r'[^A-Za-z0-9._-]+', '-', event_type.strip()).strip('-') or None


def _notification_prompt_path(event_type: str | None = None):
    safe_event_type = _safe_notification_event_type(event_type)
    if safe_event_type:
        return ensure_notification_event_prompt(safe_event_type)
    return settings.resolve_runtime_path(settings.bot_notification_prompt_path)


def _read_notification_prompt(event_type: str | None = None) -> str:
    path = _notification_prompt_path(event_type)
    if not path.exists():
        ensure_bot_files()
    return path.read_text(encoding='utf-8')


def _admin_gmail_zalo_config_payload(db: Session) -> dict[str, Any]:
    config = gmail_zalo_config(db)
    stored = db.get(IntegrationConfig, GMAIL_ZALO_CONFIG_KEY)
    payload = stored.payload if stored and isinstance(stored.payload, dict) else {}
    return {
        'enabled': bool(config.get('enabled', True)),
        'gmail_address': config.get('gmail_address'),
        'gmail_app_password_configured': bool(config.get('gmail_app_password')),
        'gmail_imap_host': config.get('gmail_imap_host'),
        'gmail_imap_port': config.get('gmail_imap_port'),
        'gmail_imap_mailbox': config.get('gmail_imap_mailbox'),
        'gmail_search_since_days': config.get('gmail_search_since_days'),
        'gmail_sale_from_addresses': config.get('gmail_sale_from_addresses'),
        'gmail_sale_subject': config.get('gmail_sale_subject'),
        'gmail_message_from_addresses': config.get('gmail_message_from_addresses'),
        'gmail_poll_max_results': config.get('gmail_poll_max_results'),
        'zalo_worker_url': config.get('zalo_worker_url'),
        'zalo_worker_token_configured': bool(config.get('zalo_worker_token')),
        'zalo_shared_secret_configured': bool(config.get('zalo_shared_secret')),
        'zalo_group_id': config.get('zalo_group_id'),
        'updated_at': stored.updated_at.isoformat() if stored and stored.updated_at else None,
        'updated_at_label': _admin_datetime_label(stored.updated_at) if stored else None,
        'stored_keys': sorted(payload.keys()),
    }


def _update_gmail_zalo_config(db: Session, update: AdminGmailZaloConfigUpdate) -> dict[str, Any]:
    stored = db.get(IntegrationConfig, GMAIL_ZALO_CONFIG_KEY)
    payload = dict(stored.payload or {}) if stored and isinstance(stored.payload, dict) else {}
    values = update.model_dump(exclude_unset=True)
    secret_fields = {'gmail_app_password', 'zalo_worker_token', 'zalo_shared_secret'}
    for key, value in values.items():
        if key in secret_fields and (value is None or str(value).strip() == ''):
            continue
        if isinstance(value, str):
            value = value.strip()
        if value is None:
            payload.pop(key, None)
            continue
        payload[key] = value

    if stored is None:
        stored = IntegrationConfig(key=GMAIL_ZALO_CONFIG_KEY, payload=payload)
    else:
        stored.payload = payload
    db.add(stored)
    db.commit()
    db.refresh(stored)
    return _admin_gmail_zalo_config_payload(db)


def _gmail_zalo_event_out(event: GmailMonitorEvent) -> dict[str, Any]:
    notification = event.notification_event
    return {
        'id': event.id,
        'gmail_message_id': event.gmail_message_id,
        'event_type': event.event_type,
        'sender': event.sender,
        'subject': event.subject,
        'snippet': event.snippet,
        'received_at': event.received_at.isoformat() if event.received_at else None,
        'received_at_label': _admin_datetime_label(event.received_at),
        'sale_order_id': event.sale_order_id,
        'sale_total_cents': event.sale_total_cents,
        'sale_currency': event.sale_currency,
        'buyer_name': event.buyer_name,
        'buyer_username': event.buyer_username,
        'order_url': event.order_url,
        'payload': event.payload,
        'notification': (
            {
                'id': notification.id,
                'event_type': notification.event_type,
                'status': notification.status.value if hasattr(notification.status, 'value') else str(notification.status),
                'attempt_count': notification.attempt_count,
                'last_error': notification.last_error,
                'delivered_at': notification.delivered_at.isoformat() if notification.delivered_at else None,
                'delivered_at_label': _admin_datetime_label(notification.delivered_at),
                'message': str((notification.payload or {}).get('message') or ''),
            }
            if notification
            else None
        ),
    }


def _gmail_zalo_recent_events(db: Session, *, limit: int = 25) -> list[dict[str, Any]]:
    events = db.scalars(
        select(GmailMonitorEvent)
        .options(joinedload(GmailMonitorEvent.notification_event))
        .order_by(GmailMonitorEvent.received_at.desc().nullslast(), GmailMonitorEvent.id.desc())
        .limit(max(1, min(limit, 100)))
    ).unique().all()
    return [_gmail_zalo_event_out(event) for event in events]


@app.get('/admin/notifications/status')
def admin_notifications_status(
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> dict[str, Any]:
    _ensure_admin(actor)

    notification_counts = {status_value.value: 0 for status_value in NotificationStatus}
    for status_value, count in db.execute(
        select(NotificationEvent.status, func.count(NotificationEvent.id)).group_by(NotificationEvent.status)
    ).all():
        key = status_value.value if hasattr(status_value, 'value') else str(status_value)
        notification_counts[key] = int(count or 0)

    reminder_counts = {
        'total': int(db.scalar(select(func.count(ReminderRule.id))) or 0),
        'enabled': int(db.scalar(select(func.count(ReminderRule.id)).where(ReminderRule.enabled.is_(True))) or 0),
        'disabled': int(db.scalar(select(func.count(ReminderRule.id)).where(ReminderRule.enabled.is_(False))) or 0),
    }

    latest_events = db.scalars(select(NotificationEvent).order_by(NotificationEvent.id.desc()).limit(8)).all()
    latest_runs = db.scalars(
        select(ReminderRun)
        .options(joinedload(ReminderRun.rule))
        .order_by(ReminderRun.id.desc())
        .limit(12)
    ).unique().all()
    latest_interactions = db.scalars(
        select(ReminderInteraction)
        .order_by(ReminderInteraction.id.desc())
        .limit(12)
    ).all()
    now = datetime.now(ZoneInfo(settings.app_timezone))
    reconcile_state = db.get(VikunjaBridgeState, 'vikunja_reconcile')
    reconcile_value = reconcile_state.value if reconcile_state and isinstance(reconcile_state.value, dict) else {}
    reconcile_last_run_at = _parse_admin_datetime(reconcile_value.get('last_run_at'))
    latest_reminder_run = db.scalar(select(ReminderRun).order_by(ReminderRun.id.desc()).limit(1))
    latest_notification_event = latest_events[0] if latest_events else None
    reconcile_seconds_since = _seconds_since(reconcile_last_run_at, now=now)
    rule_diagnostics = diagnose_reminder_rules(db, now=now)

    return {
        'config': {
            'zalo_worker_configured': bool(settings.zalo_worker_url),
            'zalo_group_id': settings.zalo_group_id,
            'notify_internal_token_configured': bool(settings.notify_internal_token),
            'reminder_tick_token_configured': reminder_internal_token_configured(),
            'vikunja_configured': settings.vikunja_enabled,
            'vikunja_project_id': settings.vikunja_project_id,
            'retry_delays': settings.notification_retry_delays,
            'max_retries': settings.notification_max_retries,
            'delivery_batch_limit': settings.notification_delivery_batch_limit,
        },
        'cron_health': {
            'now': now.isoformat(),
            'now_label': _admin_datetime_label(now),
            'vikunja_reconcile_last_run_at': reconcile_last_run_at.isoformat() if reconcile_last_run_at else None,
            'vikunja_reconcile_last_run_label': _admin_datetime_label(reconcile_last_run_at),
            'vikunja_reconcile_seconds_since': reconcile_seconds_since,
            'vikunja_reconcile_running': reconcile_seconds_since is not None and reconcile_seconds_since <= 180,
            'vikunja_reconcile_note': 'OK nếu cron chạy mỗi phút và giá trị này dưới 180 giây.',
            'last_reminder_run_at': latest_reminder_run.created_at.isoformat() if latest_reminder_run else None,
            'last_reminder_run_label': _admin_datetime_label(latest_reminder_run.created_at) if latest_reminder_run else None,
            'last_notification_event_at': latest_notification_event.created_at.isoformat() if latest_notification_event else None,
            'last_notification_event_label': _admin_datetime_label(latest_notification_event.created_at) if latest_notification_event else None,
            'timezone': settings.app_timezone,
        },
        'event_prompt_types': ADMIN_NOTIFICATION_EVENT_TYPES,
        'contacts': _admin_contact_snapshot(db),
        'rule_diagnostics': rule_diagnostics,
        'core_rule_summary': _core_daily_rule_summary(db),
        'scheduler_plan': _scheduler_install_plan(),
        'notification_counts': notification_counts,
        'reminder_counts': reminder_counts,
        'latest_events': [
            {
                'id': event.id,
                'event_type': event.event_type,
                'channel': event.channel.value,
                'target_id': event.target_id,
                'status': event.status.value,
                'attempt_count': event.attempt_count,
                'last_error': event.last_error,
                'message': str((event.payload or {}).get('message') or '')[:240],
                'created_at': event.created_at.isoformat() if event.created_at else None,
                'created_at_label': _admin_datetime_label(event.created_at),
            }
            for event in latest_events
        ],
        'latest_reminder_runs': [
            {
                'id': run.id,
                'rule_id': run.rule_id,
                'rule_name': run.rule.name if run.rule else None,
                'rule_type': run.rule.rule_type.value if run.rule else None,
                'scheduled_for': run.scheduled_for.isoformat() if run.scheduled_for else None,
                'scheduled_for_label': _admin_datetime_label(run.scheduled_for),
                'status': run.status.value if hasattr(run.status, 'value') else str(run.status),
                'notification_event_id': run.notification_event_id,
                'run_key': run.run_key,
                'acknowledged_at': run.acknowledged_at.isoformat() if run.acknowledged_at else None,
                'snoozed_until': run.snoozed_until.isoformat() if run.snoozed_until else None,
                'escalated_at': run.escalated_at.isoformat() if run.escalated_at else None,
                'created_at': run.created_at.isoformat() if run.created_at else None,
                'created_at_label': _admin_datetime_label(run.created_at),
            }
            for run in latest_runs
        ],
        'latest_reminder_interactions': [
            {
                'id': interaction.id,
                'run_id': interaction.run_id,
                'rule_id': interaction.rule_id,
                'user_id': interaction.user_id,
                'conversation_id': interaction.conversation_id,
                'message_id': interaction.message_id,
                'interaction_type': (
                    interaction.interaction_type.value
                    if hasattr(interaction.interaction_type, 'value')
                    else str(interaction.interaction_type)
                ),
                'text': interaction.text,
                'payload': interaction.payload,
                'created_at': interaction.created_at.isoformat() if interaction.created_at else None,
                'created_at_label': _admin_datetime_label(interaction.created_at),
            }
            for interaction in latest_interactions
        ],
    }


@app.get('/admin/notifications/prompt')
def admin_notifications_get_prompt(
    event_type: str | None = Query(default=None),
    actor: User = Depends(get_actor),
) -> dict[str, str]:
    _ensure_admin(actor)
    safe_event_type = _safe_notification_event_type(event_type)
    path = _notification_prompt_path(safe_event_type)
    return {
        'scope': safe_event_type or 'global',
        'path': str(path),
        'content': _read_notification_prompt(safe_event_type),
    }


@app.put('/admin/notifications/prompt')
def admin_notifications_update_prompt(
    payload: AdminNotificationPromptUpdate,
    event_type: str | None = Query(default=None),
    actor: User = Depends(get_actor),
) -> dict[str, str | int]:
    _ensure_admin(actor)
    safe_event_type = _safe_notification_event_type(event_type)
    path = _notification_prompt_path(safe_event_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = payload.content.strip() + '\n'
    path.write_text(content, encoding='utf-8')
    return {
        'scope': safe_event_type or 'global',
        'path': str(path),
        'bytes': len(content.encode('utf-8')),
        'content': content,
    }


@app.post('/admin/notifications/reminders/{reminder_id}/test')
def admin_notifications_test_reminder_rule(
    reminder_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> dict[str, Any]:
    _ensure_admin(actor)
    rule = db.get(ReminderRule, reminder_id)
    if not rule:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Reminder not found.')
    return run_reminder_rule_now(db, rule=rule)


@app.post('/admin/notifications/bootstrap-core-rules')
def admin_notifications_bootstrap_core_rules(
    cleanup_duplicates: bool = Query(default=False),
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> dict[str, Any]:
    _ensure_admin(actor)
    if not settings.zalo_group_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='ZALO_GROUP_ID is not configured, cannot bootstrap daily group digest.',
        )
    result = _bootstrap_core_daily_rules(db, actor=actor, cleanup_duplicates=cleanup_duplicates)
    result['cleanup_duplicates'] = cleanup_duplicates
    return result


@app.get('/admin/notifications/scheduler/install-plan')
def admin_notifications_scheduler_install_plan(
    actor: User = Depends(get_actor),
) -> dict[str, Any]:
    _ensure_admin(actor)
    return _scheduler_install_plan()


@app.post('/admin/notifications/test')
def admin_notifications_test(
    payload: AdminNotificationTestRequest,
    actor: User = Depends(get_actor),
) -> dict[str, Any]:
    _ensure_admin(actor)
    ok, response_status, response_body, error = send_zalo_text(
        channel=payload.channel,
        target_id=payload.target_id,
        message=payload.message,
        context={'source': 'admin_notification_ui', **payload.context},
    )
    return {
        'ok': ok,
        'status_code': response_status,
        'body': response_body,
        'error': error,
    }


@app.post('/admin/notifications/dispatch')
def admin_notifications_dispatch(
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> dict[str, int]:
    _ensure_admin(actor)
    return dispatch_due_notification_events(db)


@app.post('/admin/notifications/reconcile')
def admin_notifications_reconcile(
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> dict[str, Any]:
    _ensure_admin(actor)
    return reconcile_vikunja_bridge(db)


@app.get('/admin/integrations/gmail-zalo')
def admin_gmail_zalo_status(
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> dict[str, Any]:
    _ensure_admin(actor)
    counts = {
        'total': int(db.scalar(select(func.count(GmailMonitorEvent.id))) or 0),
        'sales': int(db.scalar(select(func.count(GmailMonitorEvent.id)).where(GmailMonitorEvent.event_type == 'sale')) or 0),
        'messages': int(
            db.scalar(select(func.count(GmailMonitorEvent.id)).where(GmailMonitorEvent.event_type == 'message')) or 0
        ),
    }
    notification_counts = {status_value.value: 0 for status_value in NotificationStatus}
    for status_value, count in db.execute(
        select(NotificationEvent.status, func.count(NotificationEvent.id))
        .where(NotificationEvent.event_type.in_(['gmail_sale_new', 'gmail_message_new', 'gmail_daily_digest']))
        .group_by(NotificationEvent.status)
    ).all():
        key = status_value.value if hasattr(status_value, 'value') else str(status_value)
        notification_counts[key] = int(count or 0)

    return {
        'config': _admin_gmail_zalo_config_payload(db),
        'counts': counts,
        'notification_counts': notification_counts,
        'recent_events': _gmail_zalo_recent_events(db, limit=25),
    }


@app.patch('/admin/integrations/gmail-zalo')
def admin_gmail_zalo_update(
    payload: AdminGmailZaloConfigUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> dict[str, Any]:
    _ensure_admin(actor)
    return {'config': _update_gmail_zalo_config(db, payload)}


@app.get('/admin/integrations/gmail-zalo/events')
def admin_gmail_zalo_events(
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> dict[str, Any]:
    _ensure_admin(actor)
    return {'events': _gmail_zalo_recent_events(db, limit=limit)}


@app.post('/admin/integrations/gmail-zalo/poll')
def admin_gmail_zalo_poll(
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> dict[str, Any]:
    _ensure_admin(actor)
    try:
        result = poll_gmail_and_notify(db)
    except GmailMonitorError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return {
        'result': result,
        'recent_events': _gmail_zalo_recent_events(db, limit=25),
    }


@app.post('/admin/integrations/gmail-zalo/test-zalo')
def admin_gmail_zalo_test_zalo(
    payload: AdminGmailZaloTestRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> dict[str, Any]:
    _ensure_admin(actor)
    config = gmail_zalo_config(db)
    target_id = str(config.get('zalo_group_id') or '').strip()
    if not target_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Zalo group ID is not configured.')
    notification_event, created = enqueue_notification_event(
        db,
        NotificationSpec(
            event_key=f'gmail-zalo-admin-test:{uuid4()}',
            event_type='gmail_zalo_admin_test',
            channel=NotificationChannel.group,
            target_id=target_id,
            payload={'message': payload.message, 'context': {'source': 'admin_gmail_zalo_ui'}},
        ),
    )
    db.commit()
    dispatch = dispatch_due_notification_events(db)
    return {
        'created': created,
        'notification_event_id': notification_event.id,
        'dispatch': dispatch,
    }


@app.post('/internal/notifications/run')
def run_internal_notifications_job(
    job: Literal['morning', 'evening'] = Query(...),
    _: None = Depends(require_internal_token),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        return run_daily_notification_job(db, job=job)
    except Exception as exc:
        logger.exception('Internal notification job failed: job=%s', job)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Notification job failed: {exc}',
        ) from exc


@app.post('/internal/gmail/poll')
def run_internal_gmail_poll(
    _: None = Depends(require_internal_token),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        return poll_gmail_and_notify(db)
    except GmailMonitorError as exc:
        logger.exception('Internal Gmail poll failed')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Gmail poll failed: {exc}',
        ) from exc


@app.post('/internal/gmail/digest')
def run_internal_gmail_digest(
    target_date: date | None = Query(default=None),
    _: None = Depends(require_internal_token),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        return run_gmail_daily_digest(db, target_date=target_date)
    except GmailMonitorError as exc:
        logger.exception('Internal Gmail digest failed')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Gmail digest failed: {exc}',
        ) from exc


@app.post('/internal/reminders/tick')
def run_internal_reminders_tick(
    _: None = Depends(require_reminder_internal_token),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        return run_reminder_tick(db)
    except Exception as exc:
        logger.exception('Internal reminder tick failed')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Reminder tick failed: {exc}',
        ) from exc


@app.post('/zalo/incoming')
def receive_zalo_incoming(
    payload: ZaloIncomingRequest,
    x_internal_secret: str | None = Header(default=None, alias='X-Internal-Secret'),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return handle_zalo_incoming(db=db, payload=payload, x_internal_secret=x_internal_secret)


@app.get('/internal/vikunja/status')
def get_internal_vikunja_status(
    _: None = Depends(require_internal_token),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return vikunja_bridge_summary(db)


@app.post('/internal/vikunja/sync-users')
def sync_internal_vikunja_users(
    _: None = Depends(require_internal_token),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_vikunja_or_503()
    return sync_vikunja_users(db, get_vikunja_client())


@app.post('/internal/vikunja/migrate-tasks')
def migrate_internal_vikunja_tasks(
    _: None = Depends(require_internal_token),
    db: Session = Depends(get_db),
    force: bool = Query(default=False),
    dry_run: bool = Query(default=False),
    limit: int | None = Query(default=None, ge=1, le=10000),
) -> dict[str, Any]:
    require_vikunja_or_503()
    return migrate_tasks_to_vikunja(db, client=get_vikunja_client(), force=force, dry_run=dry_run, limit=limit)


@app.post('/internal/vikunja/reconcile')
def reconcile_internal_vikunja(
    _: None = Depends(require_internal_token),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return reconcile_vikunja_bridge(db)


@app.post('/vikunja/webhook')
def receive_vikunja_webhook(
    payload: dict[str, Any],
    x_vikunja_secret: str | None = Header(default=None, alias='X-Vikunja-Secret'),
    x_webhook_secret: str | None = Header(default=None, alias='X-Webhook-Secret'),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    expected = settings.vikunja_webhook_secret
    if expected:
        received = x_vikunja_secret or x_webhook_secret
        if not received or not hmac.compare_digest(received, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid Vikunja webhook secret.')
    return handle_vikunja_webhook(db, payload)


@app.post('/auth/login', response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    normalized_username = payload.username.strip().lower()
    user = db.scalar(
        select(User)
        .where(func.lower(User.username) == normalized_username)
        .order_by(User.id.asc())
    )

    if not user:
        name_matches = db.scalars(
            select(User)
            .where(func.lower(func.coalesce(User.full_name, '')) == normalized_username)
            .order_by(User.id.asc())
            .limit(2)
        ).all()
        if len(name_matches) > 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Display name is duplicated. Please login with username.',
            )
        user = name_matches[0] if name_matches else None

    invalid_login_error = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid username or password.')
    if not user:
        raise invalid_login_error
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='User is inactive.')
    if not user.password_hash:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='This account has no password yet. Please use an account with password or set one first.',
        )
    if not verify_password(db, plain_password=payload.password, password_hash=user.password_hash):
        raise invalid_login_error

    access_token = create_access_token(
        user_id=user.id,
        role=user.role or 'member',
        secret_key=settings.auth_secret_key,
        expires_in_seconds=settings.auth_token_expires_minutes * 60,
    )
    return LoginResponse(access_token=access_token, user=UserOut.model_validate(user))


@app.get('/auth/me', response_model=UserOut)
def get_me(actor: User = Depends(get_actor)) -> User:
    return actor


@app.get('/users', response_model=list[UserOut])
def get_users(db: Session = Depends(get_db)) -> list[User]:
    stmt = (
        select(User)
        .where(User.is_active.is_(True))
        .order_by(func.lower(func.coalesce(User.full_name, User.username)).asc(), func.lower(User.username).asc())
    )
    return db.scalars(stmt).all()


def _ensure_reminder_manage_access(rule: ReminderRule, actor: User) -> None:
    if _is_admin(actor):
        return
    if rule.user_id == actor.id:
        return
    if rule.task:
        _ensure_task_access(rule.task, actor)
        return
    raise _forbidden('Members can only manage their own reminders.')


def _validate_reminder_payload(payload_values: dict[str, Any], actor: User, db: Session) -> None:
    target_channel = payload_values.get('target_channel')
    user_id = payload_values.get('user_id')
    task_id = payload_values.get('task_id')
    if target_channel and str(target_channel.value if hasattr(target_channel, 'value') else target_channel) == 'group' and not _is_admin(actor):
        raise _forbidden('Only admins can create group reminders.')
    if user_id and user_id != actor.id and not _is_admin(actor):
        raise _forbidden('Members can only create reminders for themselves.')
    if user_id and not db.get(User, user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Reminder user not found.')
    if task_id:
        task = db.get(Task, task_id)
        if not task:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Reminder task not found.')
        _ensure_task_access(task, actor)


@app.post('/reminders', response_model=ReminderRuleOut, status_code=status.HTTP_201_CREATED)
def create_reminder(
    payload: ReminderRuleCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> ReminderRule:
    values = payload.model_dump()
    _validate_reminder_payload(values, actor, db)
    return create_reminder_rule(db, actor=actor, values=values)


@app.get('/reminders', response_model=list[ReminderRuleOut])
def list_reminders(
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> list[ReminderRule]:
    stmt = select(ReminderRule).options(joinedload(ReminderRule.task)).order_by(ReminderRule.created_at.desc(), ReminderRule.id.desc())
    if not _is_admin(actor):
        accessible_task_ids = select(Task.id).where(Task.assigned_to == actor.id)
        stmt = stmt.where(or_(ReminderRule.user_id == actor.id, ReminderRule.task_id.in_(accessible_task_ids)))
    return db.scalars(stmt).unique().all()


@app.post('/reminders/tick')
def run_manual_reminders_tick(
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> dict[str, Any]:
    if not _is_admin(actor):
        raise _forbidden('Only admins can run reminder tick manually.')
    try:
        return run_reminder_tick(db)
    except Exception as exc:  # pragma: no cover - defensive guard for manual ops
        logger.exception('Manual reminder tick failed')
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f'Reminder tick failed: {exc}') from exc


@app.patch('/reminders/{reminder_id}', response_model=ReminderRuleOut)
def update_reminder(
    reminder_id: int,
    payload: ReminderRuleUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> ReminderRule:
    rule = db.get(ReminderRule, reminder_id)
    if not rule:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Reminder not found.')
    _ensure_reminder_manage_access(rule, actor)
    values = payload.model_dump(exclude_unset=True)
    _validate_reminder_payload(values, actor, db)
    return update_reminder_rule(db, rule=rule, values=values)


@app.delete('/reminders/{reminder_id}', response_model=ReminderRuleOut)
def disable_reminder(
    reminder_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> ReminderRule:
    rule = db.get(ReminderRule, reminder_id)
    if not rule:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Reminder not found.')
    _ensure_reminder_manage_access(rule, actor)
    rule.enabled = False
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@app.get('/shops', response_model=list[ShopOut])
def get_shops(db: Session = Depends(get_db)) -> list[Shop]:
    return db.scalars(select(Shop).order_by(Shop.name.asc())).all()


@app.post('/shops', response_model=ShopOut, status_code=status.HTTP_201_CREATED)
def create_shop(
    payload: ShopCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> Shop:
    if not _is_admin(actor):
        raise _forbidden('Only admins can manage shops.')

    name = payload.name.strip()
    existing = db.scalar(select(Shop).where(func.lower(Shop.name) == name.lower()))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Shop already exists.')

    shop = Shop(name=name)
    db.add(shop)
    db.commit()
    db.refresh(shop)
    return shop


@app.patch('/shops/{shop_id}', response_model=ShopOut)
def update_shop(
    shop_id: int,
    payload: ShopUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> Shop:
    if not _is_admin(actor):
        raise _forbidden('Only admins can manage shops.')

    shop = db.get(Shop, shop_id)
    if not shop:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Shop not found.')

    name = payload.name.strip()
    existing = db.scalar(select(Shop).where(func.lower(Shop.name) == name.lower(), Shop.id != shop_id))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Shop already exists.')

    shop.name = name
    db.add(shop)
    db.commit()
    db.refresh(shop)
    return shop


@app.delete('/shops/{shop_id}', status_code=status.HTTP_204_NO_CONTENT)
def delete_shop(
    shop_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> Response:
    if not _is_admin(actor):
        raise _forbidden('Only admins can manage shops.')

    shop = db.get(Shop, shop_id)
    if not shop:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Shop not found.')

    in_use_count = db.scalar(select(func.count(Task.id)).where(Task.shop_id == shop_id)) or 0
    if in_use_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Shop is in use by tasks and cannot be deleted.',
        )

    db.delete(shop)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get('/task-types', response_model=list[TaskTypeOut])
def get_task_types(db: Session = Depends(get_db)) -> list[TaskType]:
    return db.scalars(select(TaskType).order_by(TaskType.name.asc())).all()


@app.post('/task-types', response_model=TaskTypeOut, status_code=status.HTTP_201_CREATED)
def create_task_type(
    payload: TaskTypeCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> TaskType:
    if not _is_admin(actor):
        raise _forbidden('Only admins can manage task types.')

    name = payload.name.strip()
    existing = db.scalar(select(TaskType).where(func.lower(TaskType.name) == name.lower()))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Task type already exists.')

    task_type = TaskType(name=name)
    db.add(task_type)
    db.commit()
    db.refresh(task_type)
    return task_type


@app.patch('/task-types/{type_id}', response_model=TaskTypeOut)
def update_task_type(
    type_id: int,
    payload: TaskTypeUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> TaskType:
    if not _is_admin(actor):
        raise _forbidden('Only admins can manage task types.')

    task_type = db.get(TaskType, type_id)
    if not task_type:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task type not found.')

    name = payload.name.strip()
    existing = db.scalar(select(TaskType).where(func.lower(TaskType.name) == name.lower(), TaskType.id != type_id))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Task type already exists.')

    task_type.name = name
    db.add(task_type)
    db.commit()
    db.refresh(task_type)
    return task_type


@app.delete('/task-types/{type_id}', status_code=status.HTTP_204_NO_CONTENT)
def delete_task_type(
    type_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> Response:
    if not _is_admin(actor):
        raise _forbidden('Only admins can manage task types.')

    task_type = db.get(TaskType, type_id)
    if not task_type:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task type not found.')

    in_use_count = db.scalar(select(func.count(Task.id)).where(Task.type_id == type_id)) or 0
    if in_use_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Task type is in use by tasks and cannot be deleted.',
        )

    db.delete(task_type)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get('/tasks', response_model=TaskListResponse)
def get_tasks(
    view: str = Query(default='today', pattern='^(today|upcoming|inbox|anytime|someday|review|logbook)$'),
    assignee_id: str | None = None,
    shop_id: int | None = None,
    type_id: int | None = None,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> TaskListResponse:
    if view == 'review' and not _is_admin(actor):
        raise _forbidden('Only admins can open the review queue.')
    try:
        effective_assignee_id = assignee_id if _is_admin(actor) else None
        return list_tasks(
            db,
            view=view,
            actor_id=actor.id,
            actor_is_admin=_is_admin(actor),
            assignee_id=effective_assignee_id,
            shop_id=shop_id,
            type_id=type_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@app.patch('/tasks/reorder', status_code=status.HTTP_204_NO_CONTENT)
def reorder_tasks(
    payload: TaskReorderRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> Response:
    ordered_ids = payload.task_ids
    if not ordered_ids:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    tasks = db.scalars(select(Task).where(Task.id.in_(ordered_ids))).all()
    if not _is_admin(actor):
        for task in tasks:
            _ensure_task_access(task, actor)
    task_map = {task.id: task for task in tasks}

    for idx, task_id in enumerate(ordered_ids):
        task = task_map.get(task_id)
        if task:
            task.list_order = idx + 1

    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post('/tasks', response_model=TaskOut, status_code=status.HTTP_201_CREATED)
def create_task(
    payload: TaskCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> Task:
    values = _apply_role_on_create(payload.model_dump(), actor)
    values['title'] = values.get('title', '').strip()
    if not values['title']:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Task title cannot be empty.')
    task_type_id = values.get('type_id')
    if task_type_id is not None:
        task_type = db.get(TaskType, task_type_id)
        values['title'] = _auto_prefix_title_for_type(values['title'], task_type)
    if values.get('list_order') is None:
        values['list_order'] = next_list_order(db)

    task = Task(**values)
    db.add(task)
    db.commit()
    db.refresh(task)

    full_task = get_task_or_404(db, task.id)
    if not full_task:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail='Unable to load created task.')

    _trigger_task_created_notification(db, full_task)
    return full_task


@app.get('/tasks/{task_id}', response_model=TaskOut)
def get_task(task_id: int, db: Session = Depends(get_db), actor: User = Depends(get_actor)) -> Task:
    task = get_task_or_404(db, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')
    _ensure_task_access(task, actor)
    return task


@app.post('/tasks/{task_id}/reminders', response_model=ReminderRuleOut, status_code=status.HTTP_201_CREATED)
def create_task_reminder(
    task_id: int,
    interval_minutes: int = Query(default=60, ge=1),
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> ReminderRule:
    task = get_task_or_404(db, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')
    _ensure_task_access(task, actor)
    return create_task_nudge_rule(db, actor=actor, task=task, interval_minutes=interval_minutes)


@app.patch('/tasks/{task_id}', response_model=TaskOut)
def update_task(
    task_id: int,
    payload: TaskUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> Task:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')

    update_values = _apply_role_on_update(task, payload.model_dump(exclude_unset=True), actor)
    _validate_task_update_references(db, update_values)
    previous_status = task.status
    status_changed = 'status' in update_values and update_values['status'] != previous_status
    changed_fields = [field for field in update_values if field != 'status']
    for field, value in update_values.items():
        setattr(task, field, value)

    db.commit()
    full_task = get_task_or_404(db, task_id)
    if not full_task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')
    if status_changed:
        _trigger_status_transition_notifications(
            db,
            task=full_task,
            previous_status=previous_status,
            actor=actor,
        )
    if changed_fields:
        _trigger_task_updated_notification(db, task=full_task, actor=actor, changed_fields=changed_fields)
    return full_task


@app.patch('/tasks/{task_id}/full-edit', response_model=TaskFullEditOut)
def full_edit_task(
    task_id: int,
    payload: TaskFullEdit,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> TaskFullEditOut:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')

    payload_values = payload.model_dump(exclude_unset=True)
    attachment_links = payload_values.pop('attachment_links', [])
    update_values = _apply_role_on_update(task, payload_values, actor)
    _validate_task_update_references(db, update_values)

    if 'title' in update_values:
        update_values['title'] = update_values['title'].strip()
        if not update_values['title']:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Task title cannot be empty.')

    previous_status = task.status
    status_changed = 'status' in update_values and update_values['status'] != previous_status
    changed_fields = [field for field in update_values if field != 'status']

    for field, value in update_values.items():
        setattr(task, field, value)

    added_attachments: list[TaskAttachment] = []
    for link in attachment_links:
        attachment = _create_link_attachment_record(
            db,
            task_id=task_id,
            actor_id=actor.id,
            url=str(link['url']),
            name=link.get('name'),
        )
        added_attachments.append(attachment)

    if added_attachments:
        changed_fields.append('attachment_links')

    db.commit()
    full_task = get_task_or_404(db, task_id)
    if not full_task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')

    if status_changed:
        _trigger_status_transition_notifications(
            db,
            task=full_task,
            previous_status=previous_status,
            actor=actor,
        )
    if changed_fields:
        _trigger_task_updated_notification(db, task=full_task, actor=actor, changed_fields=changed_fields)

    created_ids = [attachment.id for attachment in added_attachments]
    created_attachments = []
    if created_ids:
        created_attachments = db.scalars(
            select(TaskAttachment)
            .where(TaskAttachment.id.in_(created_ids))
            .options(joinedload(TaskAttachment.uploader))
            .order_by(TaskAttachment.created_at.desc(), TaskAttachment.id.desc())
        ).all()

    return TaskFullEditOut(
        task=TaskOut.model_validate(full_task),
        attachments_added=[_attachment_out(attachment) for attachment in created_attachments],
    )


@app.patch('/tasks/{task_id}/status', response_model=TaskOut)
def update_task_status(
    task_id: int,
    payload: TaskStatusUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> Task:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')

    _ensure_task_access(task, actor)
    _validate_status_transition(task, payload.status, actor)
    previous_status = task.status
    task.status = payload.status
    db.commit()

    full_task = get_task_or_404(db, task_id)
    if not full_task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')
    if previous_status != full_task.status:
        _trigger_status_transition_notifications(
            db,
            task=full_task,
            previous_status=previous_status,
            actor=actor,
        )
    return full_task


@app.post('/tasks/{task_id}/convert', response_model=TaskOut, status_code=status.HTTP_201_CREATED)
def convert_task(
    task_id: int,
    payload: TaskConvertRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> Task:
    source_task = get_task_or_404(db, task_id)
    if not source_task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')

    _can_convert_task(source_task, actor)
    if source_task.status not in {TaskStatus.ready, TaskStatus.done}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Only ready or done tasks can be converted.',
        )

    target_type = db.get(TaskType, payload.target_type_id)
    if not target_type:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Target task type not found.')

    converted_task = Task(
        title=source_task.title,
        description=source_task.description,
        status=TaskStatus.todo,
        assigned_to=source_task.assigned_to,
        created_by=actor.id,
        parent_task_id=source_task.id,
        shop_id=source_task.shop_id,
        type_id=payload.target_type_id,
        scheduled_date=source_task.scheduled_date,
        due_date=source_task.due_date,
        priority=source_task.priority,
        notes=source_task.notes,
        is_someday=source_task.is_someday,
        list_order=next_list_order(db),
    )
    db.add(converted_task)
    db.flush()

    for subtask in source_task.subtasks:
        db.add(
            Subtask(
                task_id=converted_task.id,
                content=subtask.content,
                is_done=subtask.is_done,
                position=subtask.position,
            )
        )

    source_comments = db.scalars(
        select(TaskComment)
        .where(TaskComment.task_id == source_task.id)
        .order_by(TaskComment.created_at.asc(), TaskComment.id.asc())
    ).all()
    for comment in source_comments:
        db.add(
            TaskComment(
                task_id=converted_task.id,
                author_id=comment.author_id,
                content=comment.content,
                mentions=list(comment.mentions or []),
            )
        )

    source_attachments = db.scalars(
        select(TaskAttachment)
        .where(TaskAttachment.task_id == source_task.id)
        .order_by(TaskAttachment.created_at.asc(), TaskAttachment.id.asc())
    ).all()
    for attachment in source_attachments:
        db.add(
            TaskAttachment(
                task_id=converted_task.id,
                uploaded_by=attachment.uploaded_by,
                name=attachment.name,
                mime_type=attachment.mime_type,
                size_bytes=attachment.size_bytes,
                data_url=attachment.data_url,
                storage_path=attachment.storage_path,
                is_image=attachment.is_image,
            )
        )

    source_type_name = source_task.task_type.name if source_task.task_type else 'Unknown type'
    db.add(
        TaskComment(
            task_id=source_task.id,
            author_id=actor.id,
            content=f'System: Converted to task #{converted_task.id} ({target_type.name}).',
            mentions=[],
        )
    )
    db.add(
        TaskComment(
            task_id=converted_task.id,
            author_id=actor.id,
            content=f'System: Converted from task #{source_task.id} ({source_type_name}).',
            mentions=[],
        )
    )

    db.commit()

    created = get_task_or_404(db, converted_task.id)
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail='Unable to load converted task.')
    return created


@app.delete('/tasks/{task_id}', status_code=status.HTTP_204_NO_CONTENT)
def delete_task(task_id: int, db: Session = Depends(get_db), actor: User = Depends(get_actor)) -> Response:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')
    _ensure_task_access(task, actor)

    full_task = get_task_or_404(db, task_id) or task
    _trigger_task_deleted_notification(db, task=full_task, actor=actor)
    db.delete(task)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get('/tasks/{task_id}/subtasks', response_model=list[SubtaskOut])
def get_subtasks(task_id: int, db: Session = Depends(get_db), actor: User = Depends(get_actor)) -> list[Subtask]:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')
    _ensure_task_access(task, actor)

    return db.scalars(select(Subtask).where(Subtask.task_id == task_id).order_by(Subtask.position.asc())).all()


@app.post('/tasks/{task_id}/subtasks', response_model=SubtaskOut, status_code=status.HTTP_201_CREATED)
def create_subtask(
    task_id: int,
    payload: SubtaskCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> Subtask:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')
    _ensure_task_access(task, actor)

    subtask = Subtask(task_id=task_id, **payload.model_dump())
    db.add(subtask)
    db.commit()
    db.refresh(subtask)
    return subtask


@app.patch('/tasks/{task_id}/subtasks/{subtask_id}', response_model=SubtaskOut)
def update_subtask(
    task_id: int,
    subtask_id: int,
    payload: SubtaskUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> Subtask:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')
    _ensure_task_access(task, actor)

    subtask = get_subtask_or_404(db, task_id, subtask_id)
    if not subtask:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Subtask not found.')

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(subtask, field, value)

    db.commit()
    db.refresh(subtask)
    return subtask


@app.delete('/tasks/{task_id}/subtasks/{subtask_id}', status_code=status.HTTP_204_NO_CONTENT)
def delete_subtask(
    task_id: int,
    subtask_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> Response:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')
    _ensure_task_access(task, actor)

    subtask = get_subtask_or_404(db, task_id, subtask_id)
    if not subtask:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Subtask not found.')

    db.delete(subtask)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get('/tasks/{task_id}/comments', response_model=list[TaskCommentOut])
def get_task_comments(
    task_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> list[TaskComment]:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')
    _ensure_task_access(task, actor)

    stmt = (
        select(TaskComment)
        .where(TaskComment.task_id == task_id)
        .options(joinedload(TaskComment.author))
        .order_by(TaskComment.created_at.desc(), TaskComment.id.desc())
    )
    return db.scalars(stmt).all()


@app.post('/tasks/{task_id}/comments', response_model=TaskCommentOut, status_code=status.HTTP_201_CREATED)
def create_task_comment(
    task_id: int,
    payload: TaskCommentCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> TaskComment:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')
    _ensure_task_access(task, actor)

    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Comment content cannot be empty.')

    comment = TaskComment(
        task_id=task_id,
        author_id=actor.id,
        content=content,
        mentions=payload.mentions,
    )
    db.add(comment)
    db.commit()

    stmt = select(TaskComment).where(TaskComment.id == comment.id).options(joinedload(TaskComment.author))
    created = db.scalar(stmt)
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail='Unable to load created comment.')
    return created


@app.delete('/tasks/{task_id}/comments/{comment_id}', status_code=status.HTTP_204_NO_CONTENT)
def delete_task_comment(
    task_id: int,
    comment_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> Response:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')
    _ensure_task_access(task, actor)

    comment = get_task_comment_or_404(db, task_id, comment_id)
    if not comment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Comment not found.')

    db.delete(comment)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get('/tasks/{task_id}/attachments', response_model=list[TaskAttachmentOut])
def get_task_attachments(
    task_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> list[TaskAttachmentOut]:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')
    _ensure_task_access(task, actor)

    stmt = (
        select(TaskAttachment)
        .where(TaskAttachment.task_id == task_id)
        .options(joinedload(TaskAttachment.uploader))
        .order_by(TaskAttachment.created_at.desc(), TaskAttachment.id.desc())
    )
    attachments = db.scalars(stmt).all()
    return [_attachment_out(attachment) for attachment in attachments]


@app.post('/tasks/{task_id}/attachments', response_model=TaskAttachmentOut, status_code=status.HTTP_201_CREATED)
def create_task_attachment(
    task_id: int,
    file: UploadFile = File(...),
    uploaded_by: str | None = Form(default=None),
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> TaskAttachmentOut:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')
    _ensure_task_access(task, actor)
    _ = uploaded_by

    raw = file.file.read()
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Attachment file is empty.')

    size_bytes = len(raw)
    if size_bytes > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Attachment exceeds {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB limit.',
        )

    file_name = _safe_filename(file.filename or 'attachment.bin')
    mime_type = file.content_type or 'application/octet-stream'
    is_image = mime_type.startswith('image/')
    storage_path: str | None = None
    data_url = _build_data_url(mime_type, raw)

    if is_storage_enabled():
        storage_path = _build_storage_path(task_id, file_name)
        try:
            upload_bytes(storage_path, raw, mime_type)
            data_url = f'storage://{settings.supabase_storage_bucket}/{storage_path}'
        except StorageError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f'Failed to upload attachment to Supabase Storage: {exc}',
            ) from exc

    attachment = TaskAttachment(
        task_id=task_id,
        uploaded_by=actor.id,
        name=file_name,
        mime_type=mime_type,
        size_bytes=size_bytes,
        data_url=data_url,
        storage_path=storage_path,
        is_image=is_image,
    )
    db.add(attachment)
    db.commit()

    stmt = select(TaskAttachment).where(TaskAttachment.id == attachment.id).options(joinedload(TaskAttachment.uploader))
    created = db.scalar(stmt)
    if not created:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail='Unable to load created attachment.'
        )
    return _attachment_out(created)


@app.post('/tasks/{task_id}/attachments/link', response_model=TaskAttachmentOut, status_code=status.HTTP_201_CREATED)
def create_task_attachment_link(
    task_id: int,
    payload: TaskAttachmentLinkCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> TaskAttachmentOut:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')
    _ensure_task_access(task, actor)

    attachment = _create_link_attachment_record(
        db,
        task_id=task_id,
        actor_id=actor.id,
        url=str(payload.url),
        name=payload.name,
    )
    db.commit()

    stmt = select(TaskAttachment).where(TaskAttachment.id == attachment.id).options(joinedload(TaskAttachment.uploader))
    created = db.scalar(stmt)
    if not created:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail='Unable to load created attachment.'
        )
    return _attachment_out(created)


@app.delete('/tasks/{task_id}/attachments/{attachment_id}', status_code=status.HTTP_204_NO_CONTENT)
def delete_task_attachment(
    task_id: int,
    attachment_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
) -> Response:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Task not found.')
    _ensure_task_access(task, actor)

    attachment = get_task_attachment_or_404(db, task_id, attachment_id)
    if not attachment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Attachment not found.')

    if attachment.storage_path and is_storage_enabled():
        still_referenced = db.scalar(
            select(func.count(TaskAttachment.id)).where(
                TaskAttachment.storage_path == attachment.storage_path,
                TaskAttachment.id != attachment.id,
            )
        ) or 0

        if still_referenced == 0:
            try:
                delete_object(attachment.storage_path)
            except StorageError as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f'Failed to delete attachment from Supabase Storage: {exc}',
                ) from exc

    db.delete(attachment)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
