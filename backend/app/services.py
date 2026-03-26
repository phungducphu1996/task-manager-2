from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session, joinedload

from .config import get_settings
from .models import Shop, Subtask, Task, TaskAttachment, TaskComment, TaskStatus, TaskType
from .schemas import TaskGroup, TaskListResponse


def local_today() -> date:
    settings = get_settings()
    return datetime.now(ZoneInfo(settings.app_timezone)).date()


def _base_task_stmt() -> Select[tuple[Task]]:
    return (
        select(Task)
        .options(
            joinedload(Task.assignee),
            joinedload(Task.shop),
            joinedload(Task.task_type),
            joinedload(Task.subtasks),
        )
        .order_by(Task.list_order.asc(), Task.created_at.asc())
    )


def attach_latest_converted_task_ids(db: Session, tasks: list[Task]) -> None:
    if not tasks:
        return

    parent_ids = [task.id for task in tasks]
    latest_rows = db.execute(
        select(Task.parent_task_id, func.max(Task.id))
        .where(Task.parent_task_id.in_(parent_ids))
        .group_by(Task.parent_task_id)
    ).all()
    latest_by_parent = {int(parent_id): int(latest_id) for parent_id, latest_id in latest_rows if parent_id and latest_id}

    for task in tasks:
        setattr(task, 'latest_converted_task_id', latest_by_parent.get(task.id))


def _apply_filters(
    stmt: Select[tuple[Task]],
    assignee_id: str | None,
    shop_id: int | None,
    type_id: int | None,
) -> Select[tuple[Task]]:
    if assignee_id is not None:
        stmt = stmt.where(Task.assigned_to == assignee_id)
    if shop_id is not None:
        stmt = stmt.where(Task.shop_id == shop_id)
    if type_id is not None:
        stmt = stmt.where(Task.type_id == type_id)
    return stmt


def build_task_groups(view: str, tasks: list[Task], today: date) -> list[TaskGroup]:
    if view == 'today':
        overdue = [t for t in tasks if t.due_date and t.due_date < today]
        current = [
            t
            for t in tasks
            if not (t.due_date and t.due_date < today)
            and ((t.scheduled_date == today) or (t.due_date == today) or (t.scheduled_date is None and t.due_date is None))
        ]
        future = [
            t
            for t in tasks
            if not (t.due_date and t.due_date < today)
            and ((t.scheduled_date and t.scheduled_date > today) or (t.due_date and t.due_date > today))
        ]
        return [
            TaskGroup(key='today', title='Today', tasks=current),
            TaskGroup(key='overdue', title='Overdue', tasks=overdue),
            TaskGroup(key='future', title='Future', tasks=future),
        ]

    if view == 'upcoming':
        grouped: dict[date, list[Task]] = defaultdict(list)
        for task in tasks:
            target_date = task.scheduled_date or task.due_date
            if target_date:
                grouped[target_date].append(task)

        return [
            TaskGroup(key=f'upcoming-{day.isoformat()}', title=day.strftime('%A, %d %b'), date=day, tasks=grouped[day])
            for day in sorted(grouped.keys())
        ]

    if view == 'inbox':
        overdue: list[Task] = []
        today_bucket: list[Task] = []
        review_bucket: list[Task] = []
        inbox_bucket: list[Task] = []

        for task in tasks:
            if task.due_date and task.due_date < today:
                overdue.append(task)
                continue

            if task.scheduled_date == today or task.due_date == today:
                today_bucket.append(task)
                continue

            if task.status == TaskStatus.review:
                review_bucket.append(task)
                continue

            inbox_bucket.append(task)

        return [
            TaskGroup(key='overdue', title='Overdue', tasks=overdue),
            TaskGroup(key='today', title='Today', tasks=today_bucket),
            TaskGroup(key='review', title='Review', tasks=review_bucket),
            TaskGroup(key='inbox', title='Inbox', tasks=inbox_bucket),
        ]

    if view == 'anytime':
        return [TaskGroup(key='anytime', title='Anytime', tasks=tasks)]

    if view == 'someday':
        return [TaskGroup(key='someday', title='Someday', tasks=tasks)]

    if view == 'review':
        return [TaskGroup(key='review', title='Review Queue', tasks=tasks)]

    if view == 'logbook':
        return [TaskGroup(key='logbook', title='Logbook', tasks=tasks)]

    return []


def list_tasks(
    db: Session,
    view: str,
    actor_id: str | None = None,
    actor_is_admin: bool = False,
    assignee_id: str | None = None,
    shop_id: int | None = None,
    type_id: int | None = None,
) -> TaskListResponse:
    today = local_today()

    if view == 'logbook':
        stmt = _base_task_stmt().where(Task.status == TaskStatus.done)
        stmt = stmt.order_by(Task.updated_at.desc(), Task.id.desc())
    else:
        stmt = _base_task_stmt().where(Task.status != TaskStatus.done)

    if view == 'today':
        stmt = stmt.where(
            or_(
                Task.assigned_to.is_not(None),
                Task.scheduled_date == today,
                Task.due_date <= today,
                Task.scheduled_date > today,
            )
        )
    elif view == 'upcoming':
        stmt = stmt.where(or_(Task.scheduled_date > today, Task.due_date > today))
    elif view == 'inbox':
        stmt = stmt.where(
            or_(
                Task.assigned_to.is_(None),
                func.date(Task.created_at) == today,
            )
        )
    elif view == 'anytime':
        stmt = stmt.where(
            and_(
                Task.scheduled_date.is_(None),
                Task.due_date.is_(None),
                Task.is_someday.is_(False),
            )
        )
    elif view == 'someday':
        stmt = stmt.where(Task.is_someday.is_(True))
    elif view == 'review':
        stmt = stmt.where(Task.status == TaskStatus.review)
    elif view == 'logbook':
        pass
    else:
        raise ValueError('Invalid view.')

    if not actor_is_admin and actor_id:
        stmt = stmt.where(Task.assigned_to == actor_id)

    stmt = _apply_filters(stmt, assignee_id, shop_id, type_id)
    tasks = db.scalars(stmt).unique().all()
    attach_latest_converted_task_ids(db, tasks)

    groups = build_task_groups(view, tasks, today)
    groups = [group for group in groups if group.tasks]
    return TaskListResponse(view=view, groups=groups)


def next_list_order(db: Session) -> int:
    max_order = db.scalar(select(func.max(Task.list_order)))
    return (max_order or 0) + 1


def get_task_or_404(db: Session, task_id: int) -> Task | None:
    stmt = (
        _base_task_stmt()
        .where(Task.id == task_id)
        .options(joinedload(Task.subtasks))
    )
    task = db.scalars(stmt).unique().one_or_none()
    if task:
        attach_latest_converted_task_ids(db, [task])
    return task


def get_subtask_or_404(db: Session, task_id: int, subtask_id: int) -> Subtask | None:
    stmt = select(Subtask).where(Subtask.task_id == task_id, Subtask.id == subtask_id)
    return db.scalar(stmt)


def get_task_comment_or_404(db: Session, task_id: int, comment_id: int) -> TaskComment | None:
    stmt = select(TaskComment).where(TaskComment.task_id == task_id, TaskComment.id == comment_id)
    return db.scalar(stmt)


def get_task_attachment_or_404(db: Session, task_id: int, attachment_id: int) -> TaskAttachment | None:
    stmt = select(TaskAttachment).where(TaskAttachment.task_id == task_id, TaskAttachment.id == attachment_id)
    return db.scalar(stmt)


def seed_reference_data(db: Session) -> None:
    if db.scalar(select(func.count(TaskType.id))) == 0:
        db.add_all([
            TaskType(name='Design'),
            TaskType(name='Ads'),
            TaskType(name='Content'),
            TaskType(name='Customer Service'),
        ])

    if db.scalar(select(func.count(Shop.id))) == 0:
        db.add_all([Shop(name='AmzMage'), Shop(name='Yessey'), Shop(name='Gemi')])

    db.commit()
