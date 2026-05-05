from __future__ import annotations

import base64
import hmac
from logging import getLogger
from os.path import basename
from re import sub
from typing import Any, Literal
from urllib.parse import unquote, urlparse
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, inspect, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from .auth import AuthError, create_access_token, decode_access_token, extract_bearer_token, verify_password
from .bot_files import ensure_bot_files
from .config import get_settings
from .database import Base, engine, get_db
from .models import (
    BotConversationMessage,
    BotConversationState,
    BotMemoryFact,
    NotificationDelivery,
    NotificationEvent,
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
    enqueue_task_created_notifications,
    enqueue_task_deleted_notifications,
    enqueue_task_status_transition_notifications,
    enqueue_task_updated_notifications,
    is_internal_token_valid,
    run_daily_notification_job,
)
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
    is_reminder_internal_token_valid,
    reminder_internal_token_configured,
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
