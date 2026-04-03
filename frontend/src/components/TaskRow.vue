<script setup lang="ts">
import type { Task, TaskView } from '../types'

const props = withDefaults(defineProps<{
  task: Task
  selected: boolean
  checked: boolean
  view?: TaskView
}>(), {
  view: 'today'
})

const emit = defineEmits<{
  (e: 'select', task: Task): void
  (e: 'check', payload: { taskId: number; checked: boolean }): void
  (e: 'delete', task: Task): void
  (e: 'dragstart', taskId: number): void
  (e: 'drop'): void
}>()

interface MetaEntry {
  key: 'assignee' | 'shop' | 'type' | 'date'
  text: string
}

function assigneeText(task: Task): string {
  if (task.assignee?.name) return task.assignee.name
  if (task.assigned_to) return 'Assigned'
  return 'Unassigned'
}

function statusLabel(task: Task): string {
  return task.status.charAt(0).toUpperCase() + task.status.slice(1)
}

function isOverdue(task: Task): boolean {
  if (!task.due_date || task.status === 'done') return false
  const today = new Date()
  const todayIso = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`
  return task.due_date < todayIso
}

function isDueTodayOrOverdue(task: Task): boolean {
  if (!task.due_date) return false
  const today = new Date()
  const todayIso = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`
  return task.due_date <= todayIso
}

function formatDueDate(raw: string | null): string {
  if (!raw) return 'Anytime'
  const [y, m, d] = raw.split('-').map(Number)
  const target = new Date(y, (m ?? 1) - 1, d ?? 1)
  if (Number.isNaN(target.getTime())) return raw

  const today = new Date()
  const todayStart = new Date(today.getFullYear(), today.getMonth(), today.getDate())
  const tomorrow = new Date(todayStart)
  tomorrow.setDate(tomorrow.getDate() + 1)

  if (target.getTime() === todayStart.getTime()) return 'Today'
  if (target.getTime() === tomorrow.getTime()) return 'Tomorrow'

  return new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric' }).format(target)
}

function metaEntries(task: Task): MetaEntry[] {
  const entries: MetaEntry[] = [
    { key: 'assignee', text: assigneeText(task) },
    { key: 'date', text: formatDueDate(task.due_date) }
  ]

  if (task.task_type?.name) {
    entries.push({ key: 'type', text: task.task_type.name })
  }

  if (task.shop?.name) {
    entries.push({ key: 'shop', text: task.shop.name })
  }

  return entries
}

function approvalLabel(task: Task): string | null {
  if (task.status === 'review') return 'Needs approval'
  if (task.status === 'ready') return 'Approved'
  return null
}

function avatarInitial(name: string): string {
  const trimmed = name.trim()
  if (!trimmed) return '?'
  return trimmed.charAt(0).toUpperCase()
}
</script>

<template>
  <article
    class="task-row"
    :class="{
      selected: props.selected,
      checked: props.checked,
      done: props.task.status === 'done',
      overdue: isOverdue(props.task),
      'upcoming-view': props.view === 'upcoming',
      [`priority-${props.task.priority}`]: true,
    }"
    @click="emit('select', props.task)"
    @dragover.prevent
    @drop.stop.prevent="emit('drop')"
  >
    <label class="task-checkbox" @click.stop>
      <input
        type="checkbox"
        :checked="props.checked"
        @change="emit('check', { taskId: props.task.id, checked: ($event.target as HTMLInputElement).checked })"
      />
    </label>

    <div class="task-content">
      <div class="task-top-row">
        <div class="task-title-wrap">
          <h4 class="task-title" :class="{ done: props.task.status === 'done' }">{{ props.task.title }}</h4>
          <span v-if="props.view !== 'upcoming' && approvalLabel(props.task)" class="approval-chip" :class="`approval-${props.task.status}`">
            {{ approvalLabel(props.task) }}
          </span>
        </div>
        <span v-if="props.view !== 'upcoming'" class="status-chip" :class="`status-${props.task.status}`">{{ statusLabel(props.task) }}</span>
      </div>
      <div class="task-meta-row">
        <span
          v-for="entry in metaEntries(props.task)"
          :key="entry.key"
          class="meta-item"
          :class="{ 'meta-date-highlight': entry.key === 'date' && props.view === 'today' && isDueTodayOrOverdue(props.task) }"
        >
          <span v-if="entry.key === 'assignee'" class="meta-avatar" aria-hidden="true">
            <img
              v-if="props.task.assignee?.avatar_url"
              :src="props.task.assignee.avatar_url"
              :alt="props.task.assignee.name"
            />
            <span v-else>{{ avatarInitial(entry.text) }}</span>
          </span>
          <span v-else class="meta-icon" :class="`meta-icon-${entry.key}`" aria-hidden="true">
            <svg v-if="entry.key === 'shop'" viewBox="0 0 20 20" fill="none">
              <path d="M3.5 8.2 5.2 4h9.6l1.7 4.2v6.8a1 1 0 0 1-1 1H4.5a1 1 0 0 1-1-1z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>
              <path d="M8 16v-4h4v4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>
            </svg>
            <svg v-else-if="entry.key === 'type'" viewBox="0 0 20 20" fill="none">
              <path d="M3.5 10.2 9.8 3.8h6.7v6.7l-6.4 6.3z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>
              <circle cx="13.5" cy="6.4" r="1.1" fill="currentColor"/>
            </svg>
            <svg v-else viewBox="0 0 20 20" fill="none">
              <rect x="3.2" y="4.2" width="13.6" height="12.2" rx="2.1" stroke="currentColor" stroke-width="1.7"/>
              <path d="M3.2 7.4h13.6M6.1 2.9v2.6M13.9 2.9v2.6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>
            </svg>
          </span>
          {{ entry.text }}
        </span>
      </div>
    </div>

    <div class="row-controls" @click.stop>
      <button
        class="row-icon-btn drag"
        title="Drag to reorder"
        draggable="true"
        @dragstart.stop="emit('dragstart', props.task.id)"
      >
        ⋮⋮
      </button>
      <button class="row-icon-btn delete" title="Delete task" @click.stop="emit('delete', props.task)">✕</button>
    </div>
  </article>
</template>
