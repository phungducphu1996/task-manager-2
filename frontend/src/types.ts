export type UserRole = string
export type TaskStatus = 'todo' | 'doing' | 'review' | 'ready' | 'done'
export type TaskPriority = 'low' | 'medium' | 'high' | 'urgent'
export type TaskView = 'today' | 'upcoming' | 'inbox' | 'anytime' | 'someday' | 'review' | 'logbook'

export interface User {
  id: string
  name: string
  username: string
  role: UserRole | null
  zalo_user_id?: string | null
  avatar_url?: string | null
}

export interface LoginResponse {
  access_token: string
  token_type: 'bearer' | string
  user: User
}

export interface Shop {
  id: number
  name: string
}

export interface TaskType {
  id: number
  name: string
}

export interface Subtask {
  id: number
  task_id: number
  content: string
  is_done: boolean
  position: number
}

export interface TaskComment {
  id: number
  task_id: number
  author_id: string | null
  content: string
  mentions: string[]
  created_at: string
  updated_at: string
  author?: User | null
}

export interface TaskAttachment {
  id: number
  task_id: number
  uploaded_by: string | null
  name: string
  mime_type: string
  size_bytes: number
  data_url: string
  is_image: boolean
  created_at: string
  uploader?: User | null
}

export interface Task {
  id: number
  title: string
  description: string | null
  status: TaskStatus
  assigned_to: string | null
  created_by: string | null
  parent_task_id: number | null
  latest_converted_task_id?: number | null
  shop_id: number | null
  type_id: number | null
  scheduled_date: string | null
  due_date: string | null
  priority: TaskPriority
  notes: string | null
  is_someday: boolean
  list_order: number
  created_at: string
  updated_at: string
  assignee?: User | null
  shop?: Shop | null
  task_type?: TaskType | null
  subtasks: Subtask[]
}

export interface TaskGroup {
  key: string
  title: string
  date?: string | null
  tasks: Task[]
}

export interface TaskListResponse {
  view: TaskView
  groups: TaskGroup[]
}

export interface TaskPayload {
  title: string
  description?: string | null
  status?: TaskStatus
  assigned_to?: string | null
  created_by?: string | null
  shop_id?: number | null
  type_id?: number | null
  scheduled_date?: string | null
  due_date?: string | null
  priority?: TaskPriority
  notes?: string | null
  is_someday?: boolean
}

export interface TaskConvertPayload {
  target_type_id: number
}

export type ReminderRuleType = 'daily_group_digest' | 'daily_member_checkin' | 'task_nudge' | 'daily_strategy'
export type ReminderScheduleType = 'daily' | 'interval'
export type NotificationChannel = 'user' | 'group'

export interface ReminderRule {
  id: number
  name: string
  rule_type: ReminderRuleType
  enabled: boolean
  target_channel: NotificationChannel | null
  target_id: string | null
  user_id: string | null
  task_id: number | null
  schedule_type: ReminderScheduleType
  schedule_time: string | null
  interval_minutes: number | null
  timezone: string
  quiet_start: string | null
  quiet_end: string | null
  max_runs_per_day: number | null
  stop_statuses: string[]
  escalation_after_minutes: number | null
  escalation_after_runs: number | null
  payload: Record<string, unknown>
  created_by: string | null
  created_at: string
  updated_at: string
}

export interface ReminderRulePayload {
  name: string
  rule_type: ReminderRuleType
  enabled?: boolean
  target_channel?: NotificationChannel | null
  target_id?: string | null
  user_id?: string | null
  task_id?: number | null
  schedule_type?: ReminderScheduleType
  schedule_time?: string | null
  interval_minutes?: number | null
  timezone?: string | null
  quiet_start?: string | null
  quiet_end?: string | null
  max_runs_per_day?: number | null
  stop_statuses?: string[]
  escalation_after_minutes?: number | null
  escalation_after_runs?: number | null
  payload?: Record<string, unknown>
}

export interface ReminderTickResult {
  now: string
  rules_checked: number
  runs_created: number
  runs_deduped: number
  escalations_created: number
  dispatch: {
    processed: number
    sent: number
    pending: number
    failed: number
  }
}
