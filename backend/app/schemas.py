from __future__ import annotations

from datetime import date as dt_date, datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from .models import TaskPriority, TaskStatus


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    username: str
    role: str | None
    zalo_user_id: str | None = None
    avatar_url: str | None = None


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=255)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = 'bearer'
    user: UserOut


class ShopOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class ShopCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ShopUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class TaskTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class TaskTypeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class TaskTypeUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class SubtaskBase(BaseModel):
    content: str = Field(min_length=1, max_length=255)
    is_done: bool = False


class SubtaskCreate(SubtaskBase):
    position: int = 0


class SubtaskUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=255)
    is_done: bool | None = None
    position: int | None = None


class SubtaskOut(SubtaskBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    position: int


class TaskCommentBase(BaseModel):
    content: str = Field(min_length=1)
    mentions: list[str] = Field(default_factory=list)


class TaskCommentCreate(TaskCommentBase):
    author_id: str | None = None


class TaskCommentOut(TaskCommentBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    author_id: str | None
    created_at: datetime
    updated_at: datetime
    author: UserOut | None = None


class TaskAttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    uploaded_by: str | None
    name: str
    mime_type: str
    size_bytes: int
    data_url: str
    is_image: bool
    created_at: datetime
    uploader: UserOut | None = None


class TaskAttachmentLinkCreate(BaseModel):
    url: HttpUrl
    name: str | None = Field(default=None, min_length=1, max_length=255)


class TaskBase(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    status: TaskStatus = TaskStatus.todo
    assigned_to: str | None = None
    created_by: str | None = None
    shop_id: int | None = None
    type_id: int | None = None
    scheduled_date: dt_date | None = None
    due_date: dt_date | None = None
    priority: TaskPriority = TaskPriority.medium
    notes: str | None = None
    is_someday: bool = False


class TaskCreate(TaskBase):
    list_order: int | None = None


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    status: TaskStatus | None = None
    assigned_to: str | None = None
    created_by: str | None = None
    shop_id: int | None = None
    type_id: int | None = None
    scheduled_date: dt_date | None = None
    due_date: dt_date | None = None
    priority: TaskPriority | None = None
    notes: str | None = None
    is_someday: bool | None = None
    list_order: int | None = None


class TaskFullEdit(TaskUpdate):
    attachment_links: list[TaskAttachmentLinkCreate] = Field(default_factory=list)


class TaskStatusUpdate(BaseModel):
    status: TaskStatus


class TaskConvertRequest(BaseModel):
    target_type_id: int


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str | None
    status: TaskStatus
    assigned_to: str | None
    created_by: str | None
    parent_task_id: int | None = None
    latest_converted_task_id: int | None = None
    shop_id: int | None
    type_id: int | None
    scheduled_date: dt_date | None
    due_date: dt_date | None
    priority: TaskPriority
    notes: str | None
    is_someday: bool
    list_order: int
    created_at: datetime
    updated_at: datetime
    assignee: UserOut | None = None
    shop: ShopOut | None = None
    task_type: TaskTypeOut | None = None
    subtasks: list[SubtaskOut] = Field(default_factory=list)


class TaskFullEditOut(BaseModel):
    task: TaskOut
    attachments_added: list[TaskAttachmentOut] = Field(default_factory=list)


class TaskReorderRequest(BaseModel):
    task_ids: list[int]


class TaskGroup(BaseModel):
    key: str
    title: str
    date: dt_date | None = None
    tasks: list[TaskOut]


class TaskListResponse(BaseModel):
    view: str
    groups: list[TaskGroup]


class ZaloIncomingRequest(BaseModel):
    text: str = Field(min_length=1)
    from_uid: str | None = None
    conversation_id: str | None = None
    conversation_type: str | None = None
    message_id: str | None = None
    reply_to_message_id: str | None = None
    reply_to_cli_message_id: str | None = None
    quoted_text: str | None = None
    mentions: list[dict] = Field(default_factory=list)
