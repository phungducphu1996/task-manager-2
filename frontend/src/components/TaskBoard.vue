<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import SidebarNav from './SidebarNav.vue'
import TaskDetailPanel from './TaskDetailPanel.vue'
import QuickTaskComposer from './QuickTaskComposer.vue'
import TaskList from './TaskList.vue'
import { useAuthStore } from '../stores/authStore'
import { useTaskStore } from '../stores/taskStore'
import type { Task, TaskPayload, TaskView } from '../types'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const store = useTaskStore()

const detailPanelAnchor = ref<HTMLElement | null>(null)
const bulkAssigneeId = ref<string | null>(null)
const bulkDueDate = ref('')

const currentView = computed(() => store.view)
const supportedViews: TaskView[] = ['today', 'upcoming', 'inbox', 'anytime', 'review', 'logbook']
const rawRouteView = computed(() => String(route.params.view ?? 'today').toLowerCase())
const routeView = computed<TaskView>(() =>
  supportedViews.includes(rawRouteView.value as TaskView) ? (rawRouteView.value as TaskView) : 'today'
)

const headingMap: Record<TaskView, string> = {
  today: 'Today',
  upcoming: 'Upcoming',
  inbox: 'Inbox',
  anytime: 'Anytime',
  someday: 'Someday',
  logbook: 'Logbook',
  review: 'Review Queue'
}

const heading = computed(() => headingMap[currentView.value])

async function syncRouteToStore() {
  if (!supportedViews.includes(rawRouteView.value as TaskView)) {
    await router.replace({ path: '/today' })
    return
  }
  if (routeView.value === 'review' && auth.user?.role !== 'admin') {
    await router.replace({ path: '/today' })
    return
  }
  if (store.view !== routeView.value) {
    await store.setView(routeView.value)
  }
}

onMounted(async () => {
  await auth.bootstrap()
  await store.bootstrap(auth.user?.id ?? null, auth.user?.role ?? null)
  await syncRouteToStore()
})

watch(routeView, async () => {
  await syncRouteToStore()
})

watch(
  () => store.selectedTaskId,
  async (nextId, prevId) => {
    if (!nextId || String(nextId) === String(prevId)) return
    if (typeof window === 'undefined') return

    const isNarrowLayout = window.matchMedia('(max-width: 1180px)').matches
    if (!isNarrowLayout) return

    await nextTick()
    detailPanelAnchor.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
)

async function goView(view: TaskView) {
  if (view === routeView.value) return
  await router.push({ path: `/${view}` })
}

async function openManagePage() {
  await router.push({ path: '/manage' })
}

async function logout() {
  auth.logout()
  await router.replace('/login')
}

async function createQuickTask(payload: TaskPayload) {
  await store.createTask(payload)
}

async function updateTask(taskId: number, payload: Partial<TaskPayload>) {
  await store.updateTask(taskId, payload, { deferRefresh: true })
}

async function deleteTaskFromList(task: Task) {
  const accepted = window.confirm(`Delete "${task.title}"?`)
  if (!accepted) return
  await store.deleteTask(task.id)
}

async function openTaskFromLineage(taskId: number) {
  await store.openTask(taskId)
}

async function convertTaskFromDetail(
  taskId: number,
  targetTypeId: number,
  done: (errorMessage?: string) => void
) {
  try {
    await store.convertTask(taskId, targetTypeId)
    done()
  } catch (error) {
    done(error instanceof Error ? error.message : 'Failed to convert task.')
  }
}

async function applyBulkAssignee() {
  if (!store.hasBulkSelection) return
  await store.bulkUpdateSelected({ assigned_to: bulkAssigneeId.value || null })
  store.clearBulkSelection()
}

async function applyBulkDueDate() {
  if (!store.hasBulkSelection) return
  await store.bulkUpdateSelected({ due_date: bulkDueDate.value || null })
  store.clearBulkSelection()
}

async function bulkDeleteSelected() {
  if (!store.hasBulkSelection) return
  const accepted = window.confirm(`Delete ${store.selectedTaskCount} selected task(s)?`)
  if (!accepted) return
  await store.bulkDeleteSelected()
  store.clearBulkSelection()
}
</script>

<template>
  <div class="app-shell">
    <SidebarNav
      :current-view="currentView"
      :users="store.users"
      :shops="store.shops"
      :task-types="store.taskTypes"
      :assignee-id="store.assigneeId"
      :active-role="auth.user?.role ?? null"
      :shop-filter="store.shopFilter"
      :type-filter="store.typeFilter"
      :lock-assignee="auth.user?.role !== 'admin'"
      @change-view="goView"
      @change-assignee="store.setAssignee"
      @change-shop="store.setShopFilter"
      @change-type="store.setTypeFilter"
      @open-manage="openManagePage"
      @logout="logout"
    />

    <main class="main-column">
      <header class="main-header">
        <div>
          <h2 class="main-view-title" :class="{ 'upcoming-title': currentView === 'upcoming' }">
            <span v-if="currentView === 'upcoming'" class="main-view-icon" aria-hidden="true">
              <svg viewBox="0 0 20 20" fill="none">
                <rect x="2.5" y="3.5" width="15" height="13.5" rx="2.6" stroke="currentColor" stroke-width="1.9" />
                <path d="M2.5 7.4h15M6.1 1.9v2.9M13.9 1.9v2.9" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" />
                <circle cx="6.5" cy="11.6" r="1" fill="currentColor" />
                <circle cx="10" cy="11.6" r="1" fill="currentColor" />
                <circle cx="13.5" cy="11.6" r="1" fill="currentColor" />
              </svg>
            </span>
            {{ heading }}
          </h2>
        </div>

        <QuickTaskComposer
          :users="store.users"
          :shops="store.shops"
          :active-assignee-id="store.assigneeId"
          :active-role="auth.user?.role ?? null"
          :view="store.view"
          @create="createQuickTask"
        />
      </header>

      <p v-if="store.error" class="error-message">{{ store.error }}</p>
      <section v-if="store.hasBulkSelection" class="bulk-toolbar">
        <strong>{{ store.selectedTaskCount }} selected</strong>
        <div class="bulk-actions">
          <template v-if="auth.user?.role === 'admin'">
            <select v-model="bulkAssigneeId">
              <option :value="null">Unassigned</option>
              <option v-for="user in store.users" :key="user.id" :value="user.id">{{ user.name }}</option>
            </select>
            <button class="ghost-btn" @click="applyBulkAssignee">Set assignee</button>
          </template>

          <input v-model="bulkDueDate" type="date" />
          <button class="ghost-btn" @click="applyBulkDueDate">Set deadline</button>
          <button class="ghost-btn danger" @click="bulkDeleteSelected">Delete</button>
          <button class="ghost-btn" @click="store.clearBulkSelection">Clear</button>
        </div>
      </section>

      <TaskList
        :groups="store.groups"
        :view="store.view"
        :selected-task-id="store.selectedTaskId"
        :selected-task-ids="store.selectedTaskIds"
        :loading="store.loading"
        @select="store.selectTask"
        @check="store.toggleTaskSelection"
        @delete="deleteTaskFromList"
        @reorder="store.reorderInGroup"
      />
    </main>

    <div ref="detailPanelAnchor" class="detail-panel-wrap">
      <TaskDetailPanel
        :task="store.selectedTask"
        :users="store.users"
        :current-user-id="auth.user?.id ?? null"
        :is-admin="auth.user?.role === 'admin'"
        :shops="store.shops"
        :task-types="store.taskTypes"
        @update-task="updateTask"
        @delete-task="(id) => store.deleteTask(id)"
        @add-subtask="store.addSubtask"
        @update-subtask="store.updateSubtask"
        @delete-subtask="store.deleteSubtask"
        @open-task="openTaskFromLineage"
        @convert-task="convertTaskFromDetail"
      />
    </div>
  </div>
</template>
