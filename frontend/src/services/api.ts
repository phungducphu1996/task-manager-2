import type {
  LoginResponse,
  GmailZaloConfig,
  GmailZaloConfigPayload,
  GmailZaloEvent,
  GmailZaloStatus,
  ReminderRule,
  ReminderRulePayload,
  ReminderTickResult,
  Shop,
  TaskAttachment,
  TaskComment,
  TaskConvertPayload,
  Subtask,
  Task,
  TaskGroup,
  TaskListResponse,
  TaskPayload,
  TaskType,
  TaskView,
  User
} from '../types'

const API_BASE_URL =
  import.meta.env.VITE_API_URL ??
  (typeof window !== 'undefined' ? `${window.location.origin}/task-api` : 'http://localhost:8010')
const TOKEN_STORAGE_KEY = 'team_task_token'

export function getStoredToken(): string | null {
  if (typeof window === 'undefined') return null
  return window.localStorage.getItem(TOKEN_STORAGE_KEY)
}

export function setStoredToken(token: string | null) {
  if (typeof window === 'undefined') return
  if (token) {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, token)
    return
  }
  window.localStorage.removeItem(TOKEN_STORAGE_KEY)
}

interface TaskQuery {
  view: TaskView
  assignee_id?: string | null
  shop_id?: number | null
  type_id?: number | null
}

interface RequestOptions extends RequestInit {
  actorId?: string | null
}

async function request<T>(path: string, init?: RequestOptions): Promise<T> {
  const isFormData = init?.body instanceof FormData
  const token = getStoredToken()
  const headers: Record<string, string> = {
    ...(init?.headers as Record<string, string> | undefined)
  }
  if (!isFormData) headers['Content-Type'] = 'application/json'
  if (token) headers.Authorization = `Bearer ${token}`
  if (init?.actorId) headers['X-Actor-Id'] = init.actorId

  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers,
    ...init
  })

  if (!response.ok) {
    let message = ''
    try {
      const data = await response.json()
      message = typeof data?.detail === 'string' ? data.detail : JSON.stringify(data)
    } catch {
      message = await response.text()
    }
    throw new Error(message || `Request failed with status ${response.status}`)
  }

  if (response.status === 204) {
    return undefined as T
  }

  return response.json() as Promise<T>
}

export const api = {
  login: (username: string, password: string) =>
    request<LoginResponse>('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
  getMe: () => request<User>('/auth/me'),

  getUsers: () => request<User[]>('/users'),
  getShops: () => request<Shop[]>('/shops'),
  createShop: (payload: { name: string }) => request<Shop>('/shops', { method: 'POST', body: JSON.stringify(payload) }),
  updateShop: (id: number, payload: { name: string }) =>
    request<Shop>(`/shops/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteShop: (id: number) => request<void>(`/shops/${id}`, { method: 'DELETE' }),
  getTaskTypes: () => request<TaskType[]>('/task-types'),
  createTaskType: (payload: { name: string }) =>
    request<TaskType>('/task-types', { method: 'POST', body: JSON.stringify(payload) }),
  updateTaskType: (id: number, payload: { name: string }) =>
    request<TaskType>(`/task-types/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteTaskType: (id: number) => request<void>(`/task-types/${id}`, { method: 'DELETE' }),

  getReminders: (actorId: string | null) => request<ReminderRule[]>('/reminders', { actorId }),
  createReminder: (payload: ReminderRulePayload, actorId: string | null) =>
    request<ReminderRule>('/reminders', { method: 'POST', body: JSON.stringify(payload), actorId }),
  updateReminder: (id: number, payload: Partial<ReminderRulePayload>, actorId: string | null) =>
    request<ReminderRule>(`/reminders/${id}`, { method: 'PATCH', body: JSON.stringify(payload), actorId }),
  deleteReminder: (id: number, actorId: string | null) =>
    request<ReminderRule>(`/reminders/${id}`, { method: 'DELETE', actorId }),
  runReminderTick: (actorId: string | null) =>
    request<ReminderTickResult>('/reminders/tick', { method: 'POST', actorId }),

  getTasks: (query: TaskQuery, actorId: string | null) => {
    const params = new URLSearchParams({ view: query.view })
    if (query.assignee_id) params.set('assignee_id', String(query.assignee_id))
    if (query.shop_id) params.set('shop_id', String(query.shop_id))
    if (query.type_id) params.set('type_id', String(query.type_id))
    return request<TaskListResponse>(`/tasks?${params.toString()}`, { actorId })
  },

  createTask: (payload: TaskPayload, actorId: string | null) =>
    request<Task>('/tasks', { method: 'POST', body: JSON.stringify(payload), actorId }),
  getTask: (id: number, actorId: string | null) => request<Task>(`/tasks/${id}`, { actorId }),
  updateTask: (id: number, payload: Partial<TaskPayload>, actorId: string | null) =>
    request<Task>(`/tasks/${id}`, { method: 'PATCH', body: JSON.stringify(payload), actorId }),
  convertTask: (id: number, payload: TaskConvertPayload, actorId: string | null) =>
    request<Task>(`/tasks/${id}/convert`, { method: 'POST', body: JSON.stringify(payload), actorId }),
  updateTaskStatus: (id: number, status: Task['status'], actorId: string | null) =>
    request<Task>(`/tasks/${id}/status`, { method: 'PATCH', body: JSON.stringify({ status }), actorId }),
  reorderTasks: (taskIds: number[], actorId: string | null) =>
    request<void>('/tasks/reorder', { method: 'PATCH', body: JSON.stringify({ task_ids: taskIds }), actorId }),
  deleteTask: (id: number, actorId: string | null) => request<void>(`/tasks/${id}`, { method: 'DELETE', actorId }),

  getSubtasks: (taskId: number, actorId: string | null) =>
    request<Subtask[]>(`/tasks/${taskId}/subtasks`, { actorId }),
  createSubtask: (taskId: number, payload: { content: string; position?: number }, actorId: string | null) =>
    request<Subtask>(`/tasks/${taskId}/subtasks`, { method: 'POST', body: JSON.stringify(payload), actorId }),
  updateSubtask: (taskId: number, subtaskId: number, payload: Partial<Subtask>, actorId: string | null) =>
    request<Subtask>(`/tasks/${taskId}/subtasks/${subtaskId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
      actorId
    }),
  deleteSubtask: (taskId: number, subtaskId: number, actorId: string | null) =>
    request<void>(`/tasks/${taskId}/subtasks/${subtaskId}`, { method: 'DELETE', actorId }),

  getTaskComments: (taskId: number, actorId: string | null) =>
    request<TaskComment[]>(`/tasks/${taskId}/comments`, { actorId }),
  createTaskComment: (
    taskId: number,
    payload: { author_id?: string | null; content: string; mentions?: string[] },
    actorId: string | null
  ) => request<TaskComment>(`/tasks/${taskId}/comments`, { method: 'POST', body: JSON.stringify(payload), actorId }),
  deleteTaskComment: (taskId: number, commentId: number, actorId: string | null) =>
    request<void>(`/tasks/${taskId}/comments/${commentId}`, { method: 'DELETE', actorId }),

  getTaskAttachments: (taskId: number, actorId: string | null) =>
    request<TaskAttachment[]>(`/tasks/${taskId}/attachments`, { actorId }),
  createTaskAttachment: (
    taskId: number,
    payload: {
      uploaded_by?: string | null
      file: File
    },
    actorId: string | null
  ) => {
    const form = new FormData()
    if (payload.uploaded_by) form.set('uploaded_by', payload.uploaded_by)
    form.set('file', payload.file)
    return request<TaskAttachment>(`/tasks/${taskId}/attachments`, { method: 'POST', body: form, actorId })
  },
  createTaskAttachmentLink: (
    taskId: number,
    payload: {
      url: string
      name?: string | null
    },
    actorId: string | null
  ) => request<TaskAttachment>(`/tasks/${taskId}/attachments/link`, { method: 'POST', body: JSON.stringify(payload), actorId }),
  deleteTaskAttachment: (taskId: number, attachmentId: number, actorId: string | null) =>
    request<void>(`/tasks/${taskId}/attachments/${attachmentId}`, { method: 'DELETE', actorId }),

  getGmailZaloStatus: () => request<GmailZaloStatus>('/admin/integrations/gmail-zalo'),
  updateGmailZaloConfig: (payload: GmailZaloConfigPayload) =>
    request<{ config: GmailZaloConfig }>('/admin/integrations/gmail-zalo', {
      method: 'PATCH',
      body: JSON.stringify(payload)
    }),
  getGmailZaloEvents: (limit = 50) =>
    request<{ events: GmailZaloEvent[] }>(`/admin/integrations/gmail-zalo/events?limit=${limit}`),
  pollGmailZalo: () =>
    request<{
      result: {
        fetched: number
        created: number
        skipped: number
        detected: Record<string, number>
        dispatch: Record<string, number>
      }
      recent_events: GmailZaloEvent[]
    }>('/admin/integrations/gmail-zalo/poll', { method: 'POST' }),
  testGmailZalo: (message: string) =>
    request<{ created: boolean; notification_event_id: number; dispatch: Record<string, number> }>(
      '/admin/integrations/gmail-zalo/test-zalo',
      { method: 'POST', body: JSON.stringify({ message }) }
    )
}

export function flattenGroups(groups: TaskGroup[]): Task[] {
  return groups.flatMap((group) => group.tasks)
}
