from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Enum, ForeignKey, Integer, String, Text, Uuid, cast, func
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship

from .database import Base
from .config import get_settings

settings = get_settings()
social_user_schema = None if settings.database_url.startswith('sqlite') else 'social'


class TaskStatus(str, enum.Enum):
    todo = 'todo'
    doing = 'doing'
    review = 'review'
    ready = 'ready'
    done = 'done'


class TaskPriority(str, enum.Enum):
    low = 'low'
    medium = 'medium'
    high = 'high'
    urgent = 'urgent'


class NotificationChannel(str, enum.Enum):
    user = 'user'
    group = 'group'


class NotificationStatus(str, enum.Enum):
    pending = 'pending'
    sent = 'sent'
    failed = 'failed'
    skipped = 'skipped'


class User(Base):
    __tablename__ = 'users'
    __table_args__ = {'schema': social_user_schema} if social_user_schema else {}

    id: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(120), nullable=False)
    full_name: Mapped[str | None] = mapped_column('name', String(120), nullable=True)
    zalo_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    @property
    def name(self) -> str:
        candidate = (self.full_name or '').strip()
        return candidate or self.username


class Shop(Base):
    __tablename__ = 'shops'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)


class TaskType(Base):
    __tablename__ = 'task_types'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)


class Task(Base):
    __tablename__ = 'tasks'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name='task_status', native_enum=False, validate_strings=True),
        nullable=False,
        default=TaskStatus.todo,
    )

    assigned_to: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    created_by: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    parent_task_id: Mapped[int | None] = mapped_column(ForeignKey('tasks.id', ondelete='SET NULL'), nullable=True, index=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey('shops.id'), nullable=True)
    type_id: Mapped[int | None] = mapped_column(ForeignKey('task_types.id'), nullable=True)

    scheduled_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    priority: Mapped[TaskPriority] = mapped_column(
        Enum(TaskPriority, name='task_priority', native_enum=False, validate_strings=True),
        nullable=False,
        default=TaskPriority.medium,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_someday: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    list_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    assignee: Mapped[User | None] = relationship(
        'User',
        primaryjoin=lambda: cast(foreign(Task.assigned_to), String(64)) == User.id,
        viewonly=True,
    )
    creator: Mapped[User | None] = relationship(
        'User',
        primaryjoin=lambda: cast(foreign(Task.created_by), String(64)) == User.id,
        viewonly=True,
    )
    parent_task: Mapped[Task | None] = relationship(
        'Task',
        remote_side=lambda: [Task.id],
        foreign_keys=lambda: [Task.parent_task_id],
        back_populates='converted_tasks',
    )
    converted_tasks: Mapped[list[Task]] = relationship(
        'Task',
        foreign_keys=lambda: [Task.parent_task_id],
        back_populates='parent_task',
        order_by='Task.created_at.desc(), Task.id.desc()',
    )
    shop: Mapped[Shop | None] = relationship('Shop')
    task_type: Mapped[TaskType | None] = relationship('TaskType')
    subtasks: Mapped[list[Subtask]] = relationship(
        'Subtask',
        back_populates='task',
        cascade='all, delete-orphan',
        order_by='Subtask.position',
    )
    comments: Mapped[list[TaskComment]] = relationship(
        'TaskComment',
        back_populates='task',
        cascade='all, delete-orphan',
        order_by='TaskComment.created_at.desc(), TaskComment.id.desc()',
    )
    attachments: Mapped[list[TaskAttachment]] = relationship(
        'TaskAttachment',
        back_populates='task',
        cascade='all, delete-orphan',
        order_by='TaskAttachment.created_at.desc(), TaskAttachment.id.desc()',
    )


class Subtask(Base):
    __tablename__ = 'subtasks'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey('tasks.id', ondelete='CASCADE'), nullable=False, index=True)
    content: Mapped[str] = mapped_column(String(255), nullable=False)
    is_done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    task: Mapped[Task] = relationship('Task', back_populates='subtasks')


class TaskComment(Base):
    __tablename__ = 'task_comments'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey('tasks.id', ondelete='CASCADE'), nullable=False, index=True)
    author_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    mentions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    task: Mapped[Task] = relationship('Task', back_populates='comments')
    author: Mapped[User | None] = relationship(
        'User',
        primaryjoin=lambda: cast(foreign(TaskComment.author_id), String(64)) == User.id,
        viewonly=True,
    )


class TaskAttachment(Base):
    __tablename__ = 'task_attachments'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey('tasks.id', ondelete='CASCADE'), nullable=False, index=True)
    uploaded_by: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    data_url: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_image: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    task: Mapped[Task] = relationship('Task', back_populates='attachments')
    uploader: Mapped[User | None] = relationship(
        'User',
        primaryjoin=lambda: cast(foreign(TaskAttachment.uploaded_by), String(64)) == User.id,
        viewonly=True,
    )


class NotificationEvent(Base):
    __tablename__ = 'notification_events'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    event_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    channel: Mapped[NotificationChannel] = mapped_column(
        Enum(NotificationChannel, name='notification_channel', native_enum=False, validate_strings=True),
        nullable=False,
    )
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey('tasks.id', ondelete='SET NULL'), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus, name='notification_status', native_enum=False, validate_strings=True),
        nullable=False,
        default=NotificationStatus.pending,
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    task: Mapped[Task | None] = relationship('Task')
    user: Mapped[User | None] = relationship(
        'User',
        primaryjoin=lambda: foreign(NotificationEvent.user_id) == User.id,
        viewonly=True,
    )
    deliveries: Mapped[list[NotificationDelivery]] = relationship(
        'NotificationDelivery',
        back_populates='event',
        cascade='all, delete-orphan',
        order_by='NotificationDelivery.attempt.asc(), NotificationDelivery.id.asc()',
    )


class NotificationDelivery(Base):
    __tablename__ = 'notification_deliveries'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey('notification_events.id', ondelete='CASCADE'), nullable=False, index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    request_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    event: Mapped[NotificationEvent] = relationship('NotificationEvent', back_populates='deliveries')


class ZaloIncomingCommand(Base):
    __tablename__ = 'zalo_incoming_commands'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    message_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    message_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    conversation_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    from_uid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    command: Mapped[str] = mapped_column(String(32), nullable=False)
    task_id: Mapped[int | None] = mapped_column(ForeignKey('tasks.id', ondelete='SET NULL'), nullable=True, index=True)
    response_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    task: Mapped[Task | None] = relationship('Task')


class BotConversationMessage(Base):
    __tablename__ = 'bot_conversation_messages'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    message_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict] = mapped_column('metadata', JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user: Mapped[User | None] = relationship(
        'User',
        primaryjoin=lambda: foreign(BotConversationMessage.user_id) == User.id,
        viewonly=True,
    )


class BotMemoryFact(Base):
    __tablename__ = 'bot_memory_facts'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    fact: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    source_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped[User | None] = relationship(
        'User',
        primaryjoin=lambda: foreign(BotMemoryFact.user_id) == User.id,
        viewonly=True,
    )
