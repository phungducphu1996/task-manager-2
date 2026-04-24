from datetime import date, datetime, timedelta

from app.models import Task, TaskPriority, TaskStatus
from app.services import build_task_groups


def make_task(
    title: str,
    *,
    scheduled_date=None,
    due_date=None,
    status: TaskStatus = TaskStatus.todo,
    task_id: int = 1,
):
    now = datetime.now()
    return Task(
        id=task_id,
        title=title,
        status=status,
        priority=TaskPriority.medium,
        list_order=0,
        is_someday=False,
        scheduled_date=scheduled_date,
        due_date=due_date,
        created_at=now,
        updated_at=now,
    )


def test_build_today_groups() -> None:
    today = date(2026, 3, 19)

    overdue_task = make_task('Overdue', due_date=today - timedelta(days=1), task_id=1)
    today_task = make_task('Today', scheduled_date=today, task_id=2)
    future_task = make_task('Future', scheduled_date=today + timedelta(days=2), task_id=3)

    groups = build_task_groups('today', [today_task, overdue_task, future_task], today)
    group_map = {group.key: group.tasks for group in groups}

    assert [task.title for task in group_map['overdue']] == ['Overdue']
    assert [task.title for task in group_map['today']] == ['Today']
    assert 'future' not in group_map


def test_build_upcoming_groups_sorted_by_date() -> None:
    today = date(2026, 3, 19)

    next_day = make_task('Next day', scheduled_date=today + timedelta(days=1), task_id=1)
    later = make_task('Later', scheduled_date=today + timedelta(days=5), task_id=2)

    groups = build_task_groups('upcoming', [later, next_day], today)

    assert len(groups) == 2
    assert groups[0].tasks[0].title == 'Next day'
    assert groups[1].tasks[0].title == 'Later'


def test_build_inbox_groups_with_exclusive_priority_order() -> None:
    today = date(2026, 3, 19)

    overdue = make_task('Overdue first', due_date=today - timedelta(days=1), status=TaskStatus.review, task_id=1)
    today_due = make_task('Today due', due_date=today, status=TaskStatus.review, task_id=2)
    future = make_task('Future bucket', due_date=today + timedelta(days=2), status=TaskStatus.review, task_id=3)
    plain = make_task('Inbox bucket', task_id=4)

    groups = build_task_groups('inbox', [plain, future, today_due, overdue], today)

    assert [group.key for group in groups] == ['today', 'overdue', 'future', 'anytime']
    assert [task.title for task in groups[0].tasks] == ['Today due']
    assert [task.title for task in groups[1].tasks] == ['Overdue first']
    assert [task.title for task in groups[2].tasks] == ['Future bucket']
    assert [task.title for task in groups[3].tasks] == ['Inbox bucket']
