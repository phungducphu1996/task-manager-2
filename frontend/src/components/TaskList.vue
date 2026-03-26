<script setup lang="ts">
import { ref } from 'vue'
import TaskRow from './TaskRow.vue'
import type { Task, TaskGroup, TaskView } from '../types'

const props = withDefaults(defineProps<{
  groups: TaskGroup[]
  selectedTaskId: number | string | null
  selectedTaskIds: number[]
  loading: boolean
  view?: TaskView
}>(), {
  view: 'today'
})

const emit = defineEmits<{
  (e: 'select', task: Task): void
  (e: 'check', payload: { taskId: number; checked: boolean }): void
  (e: 'delete', task: Task): void
  (e: 'reorder', taskIds: number[]): void
}>()

const draggedTaskId = ref<number | null>(null)
const draggedGroupKey = ref<string | null>(null)

function onDragStart(groupKey: string, taskId: number) {
  draggedGroupKey.value = groupKey
  draggedTaskId.value = taskId
}

function onDrop(group: TaskGroup, targetIndex: number) {
  if (!draggedTaskId.value || draggedGroupKey.value !== group.key) return

  const fromIndex = group.tasks.findIndex((task) => task.id === draggedTaskId.value)
  if (fromIndex === -1 || fromIndex === targetIndex) return

  const ordered = [...group.tasks]
  const [item] = ordered.splice(fromIndex, 1)
  ordered.splice(targetIndex, 0, item)

  emit('reorder', ordered.map((task) => task.id))
  draggedTaskId.value = null
  draggedGroupKey.value = null
}

function parseGroupDate(group: TaskGroup): Date | null {
  const dateRaw =
    group.date ??
    (group.key.match(/^\d{4}-\d{2}-\d{2}$/) ? group.key : null)

  if (!dateRaw) return null
  const parsed = new Date(`${dateRaw}T00:00:00`)
  if (Number.isNaN(parsed.getTime())) return null
  return parsed
}

function upcomingDayNumber(group: TaskGroup): string {
  const parsed = parseGroupDate(group)
  if (!parsed) return group.title
  return String(parsed.getDate())
}

function upcomingDayLabel(group: TaskGroup): string {
  const parsed = parseGroupDate(group)
  if (!parsed) return group.title

  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const tomorrow = new Date(today)
  tomorrow.setDate(tomorrow.getDate() + 1)

  if (parsed.getTime() === today.getTime()) return 'Today'
  if (parsed.getTime() === tomorrow.getTime()) return 'Tomorrow'

  return new Intl.DateTimeFormat('en-US', { weekday: 'long' }).format(parsed)
}
</script>

<template>
  <section class="task-list-wrap" :class="{ 'upcoming-layout': props.view === 'upcoming' }">
    <div v-if="props.loading" class="loading-state">Loading tasks...</div>

    <template v-else>
      <section v-for="group in props.groups" :key="group.key" class="task-group">
        <header class="task-group-header" :class="{ 'upcoming-group-header': props.view === 'upcoming' }">
          <template v-if="props.view === 'upcoming'">
            <span class="upcoming-day-number">{{ upcomingDayNumber(group) }}</span>
            <h3 class="upcoming-day-label">{{ upcomingDayLabel(group) }}</h3>
            <span class="upcoming-group-line" aria-hidden="true" />
          </template>
          <template v-else>
            <h3>{{ group.title }}</h3>
            <small>{{ group.tasks.length }}</small>
          </template>
        </header>

        <TaskRow
          v-for="(task, index) in group.tasks"
          :key="task.id"
          :task="task"
          :view="props.view"
          :selected="String(task.id) === String(props.selectedTaskId)"
          :checked="props.selectedTaskIds.includes(task.id)"
          @select="emit('select', $event)"
          @check="emit('check', $event)"
          @delete="emit('delete', $event)"
          @dragstart="onDragStart(group.key, $event)"
          @drop="onDrop(group, index)"
        />
      </section>

      <div v-if="props.groups.length === 0" class="empty-state">
        <h4>Nothing to execute right now</h4>
        <p>Create a task or switch to another view.</p>
      </div>
    </template>
  </section>
</template>
