import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { api, flattenGroups } from '../services/api'
import type { Shop, Subtask, Task, TaskPayload, TaskType, TaskView, User } from '../types'

function todayIso() {
  return new Date().toISOString().slice(0, 10)
}

function byNameAsc<T extends { name: string }>(a: T, b: T) {
  return a.name.localeCompare(b.name)
}

export const useTaskStore = defineStore('tasks', () => {
  const users = ref<User[]>([])
  const shops = ref<Shop[]>([])
  const taskTypes = ref<TaskType[]>([])
  const groups = ref<{ key: string; title: string; date?: string | null; tasks: Task[] }[]>([])

  const view = ref<TaskView>('today')
  const viewCounts = ref<Partial<Record<TaskView, number>>>({})
  const assigneeId = ref<string | null>(null)
  const shopFilter = ref<number | null>(null)
  const typeFilter = ref<number | null>(null)
  const actorId = ref<string | null>(null)
  const actorRole = ref<User['role'] | null>(null)
  const selectedTaskIds = ref<number[]>([])

  const selectedTask = ref<Task | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const typeManageLoading = ref(false)
  const typeManageError = ref<string | null>(null)

  const allTasks = computed(() => flattenGroups(groups.value))
  const selectedTaskId = computed<number | string | null>(() => selectedTask.value?.id ?? null)
  const activeUser = computed(() => users.value.find((item) => item.id === assigneeId.value) ?? null)
  const isAdmin = computed(() => actorRole.value === 'admin')
  const canAssignTasks = computed(() => isAdmin.value)
  const canApproveTasks = computed(() => isAdmin.value)
  const selectedTaskCount = computed(() => selectedTaskIds.value.length)
  const hasBulkSelection = computed(() => selectedTaskIds.value.length > 0)
  let deferredFetchTimer: ReturnType<typeof setTimeout> | null = null
  let createTaskLock = false
  let lastCreateFingerprint: string | null = null
  let lastCreateAt = 0
  let viewCountFetchSeq = 0

  function countTasksFromGroups(groupList: { tasks: Task[] }[]): number {
    return groupList.reduce((sum, group) => sum + group.tasks.length, 0)
  }

  async function refreshViewCounts() {
    if (!isAdmin.value && !assigneeId.value) return

    const seq = ++viewCountFetchSeq
    const views: TaskView[] = ['inbox', 'today', 'upcoming', 'anytime', 'logbook']
    if (isAdmin.value) views.push('review')

    const effectiveAssigneeId = isAdmin.value ? assigneeId.value : null
    const responses = await Promise.allSettled(
      views.map(async (targetView) => {
        const data = await api.getTasks(
          {
            view: targetView,
            assignee_id: effectiveAssigneeId,
            shop_id: shopFilter.value,
            type_id: typeFilter.value
          },
          null
        )
        return [targetView, countTasksFromGroups(data.groups)] as const
      })
    )

    if (seq !== viewCountFetchSeq) return

    const nextCounts: Partial<Record<TaskView, number>> = {}
    responses.forEach((result) => {
      if (result.status === 'fulfilled') {
        const [targetView, count] = result.value
        nextCounts[targetView] = count
      }
    })
    viewCounts.value = { ...viewCounts.value, ...nextCounts }
  }

  async function bootstrap(initialUserId: string | null = null, currentActorRole: User['role'] | null = null) {
    loading.value = true
    error.value = null
    try {
      actorId.value = initialUserId
      actorRole.value = currentActorRole
      const [u, s, tt] = await Promise.all([api.getUsers(), api.getShops(), api.getTaskTypes()])
      users.value = u
      shops.value = s
      taskTypes.value = tt

      if (isAdmin.value) {
        assigneeId.value = null
      } else if (initialUserId && users.value.some((item) => item.id === initialUserId)) {
        assigneeId.value = initialUserId
      } else if (!assigneeId.value && users.value.length > 0) {
        const preferredAdmin = users.value.find((user) => user.role === 'admin')
        assigneeId.value = preferredAdmin?.id ?? users.value[0].id
      }

      await fetchTasks()
    } catch (err) {
      error.value = err instanceof Error ? err.message : 'Failed to load app data.'
    } finally {
      loading.value = false
    }
  }

  async function fetchTasks() {
    if (!isAdmin.value && !assigneeId.value) return
    loading.value = true
    error.value = null
    try {
      const data = await api.getTasks(
        {
          view: view.value,
          assignee_id: isAdmin.value ? assigneeId.value : null,
          shop_id: shopFilter.value,
          type_id: typeFilter.value
        },
        null
      )
      groups.value = data.groups
      viewCounts.value = {
        ...viewCounts.value,
        [view.value]: countTasksFromGroups(data.groups)
      }
      void refreshViewCounts()
      const visibleTaskIds = new Set(flattenGroups(data.groups).map((task) => task.id))
      selectedTaskIds.value = selectedTaskIds.value.filter((taskId) => visibleTaskIds.has(taskId))

      if (selectedTask.value) {
        const nextSelected = flattenGroups(data.groups).find(
          (task) => String(task.id) === String(selectedTask.value?.id)
        )
        selectedTask.value = nextSelected ?? null
      }
    } catch (err) {
      error.value = err instanceof Error ? err.message : 'Failed to load tasks.'
    } finally {
      loading.value = false
    }
  }

  function scheduleDeferredFetch(delayMs = 700) {
    if (deferredFetchTimer) clearTimeout(deferredFetchTimer)
    deferredFetchTimer = setTimeout(() => {
      void fetchTasks()
      deferredFetchTimer = null
    }, delayMs)
  }

  async function setView(next: TaskView) {
    if (next === 'review' && !isAdmin.value) {
      error.value = 'Only admins can open the review queue.'
      return
    }
    view.value = next
    selectedTaskIds.value = []
    await fetchTasks()
  }

  async function setAssignee(next: string | null) {
    if (!isAdmin.value && !next) return
    assigneeId.value = next
    if (view.value === 'review' && !isAdmin.value) {
      view.value = 'today'
    }
    selectedTaskIds.value = []
    await fetchTasks()
  }

  async function setShopFilter(next: number | null) {
    shopFilter.value = next
    selectedTaskIds.value = []
    await fetchTasks()
  }

  async function setTypeFilter(next: number | null) {
    typeFilter.value = next
    selectedTaskIds.value = []
    await fetchTasks()
  }

  function selectTask(task: Task | null) {
    selectedTask.value = task
  }

  function toggleTaskSelection(payload: { taskId: number; checked: boolean }) {
    const { taskId, checked } = payload
    const existing = new Set(selectedTaskIds.value)
    if (checked) {
      existing.add(taskId)
    } else {
      existing.delete(taskId)
    }
    selectedTaskIds.value = Array.from(existing)
  }

  function clearBulkSelection() {
    selectedTaskIds.value = []
  }

  async function createTask(input: string | TaskPayload) {
    if (createTaskLock) return
    if (!actorId.value) return
    if (!isAdmin.value && !assigneeId.value) return
    const payload: TaskPayload =
      typeof input === 'string'
        ? {
            title: input
          }
        : { ...input }

    payload.title = payload.title.trim()
    if (!payload.title) return

    const createFingerprint = JSON.stringify({
      title: payload.title.toLowerCase(),
      assigned_to: payload.assigned_to ?? null,
      shop_id: payload.shop_id ?? null,
      type_id: payload.type_id ?? null,
      due_date: payload.due_date ?? null,
      priority: payload.priority ?? null
    })
    const now = Date.now()
    if (lastCreateFingerprint === createFingerprint && now - lastCreateAt < 1500) return

    if (payload.assigned_to === undefined) payload.assigned_to = assigneeId.value
    if (!isAdmin.value) payload.assigned_to = assigneeId.value
    if (payload.created_by === undefined) payload.created_by = actorId.value
    if (payload.due_date === undefined) payload.due_date = view.value === 'today' ? todayIso() : null
    if (payload.scheduled_date === undefined) payload.scheduled_date = null
    if (payload.is_someday === undefined) payload.is_someday = false

    createTaskLock = true
    try {
      await api.createTask(payload, null)
      lastCreateFingerprint = createFingerprint
      lastCreateAt = now
      await fetchTasks()
    } finally {
      createTaskLock = false
    }
  }

  async function updateTask(taskId: number, payload: Partial<TaskPayload>, options?: { deferRefresh?: boolean }) {
    await api.updateTask(taskId, payload, null)
    if (selectedTask.value && String(selectedTask.value.id) === String(taskId)) {
      selectedTask.value = {
        ...selectedTask.value,
        ...payload
      }
    }
    if (options?.deferRefresh) {
      scheduleDeferredFetch()
      return
    }
    await fetchTasks()
    selectedTask.value = flattenGroups(groups.value).find((task) => String(task.id) === String(taskId)) ?? null
  }

  async function openTask(taskId: number) {
    const inCurrentGroups = flattenGroups(groups.value).find((item) => item.id === taskId) ?? null
    if (inCurrentGroups) {
      selectedTask.value = inCurrentGroups
      return
    }
    selectedTask.value = await api.getTask(taskId, null)
  }

  async function convertTask(taskId: number, targetTypeId: number) {
    const created = await api.convertTask(taskId, { target_type_id: targetTypeId }, null)
    await fetchTasks()
    selectedTask.value = flattenGroups(groups.value).find((task) => task.id === created.id) ?? created
    return created
  }

  async function toggleTaskDone(task: Task) {
    if (!isAdmin.value) return
    const nextStatus =
      task.status === 'review'
        ? 'ready'
        : task.status === 'ready'
          ? 'done'
          : task.status === 'done'
            ? 'todo'
            : 'done'
    await api.updateTaskStatus(task.id, nextStatus, null)
    await fetchTasks()
  }

  async function bulkUpdateSelected(payload: Partial<TaskPayload>) {
    if (selectedTaskIds.value.length === 0) return
    for (const taskId of selectedTaskIds.value) {
      await api.updateTask(taskId, payload, null)
    }
    await fetchTasks()
  }

  async function bulkDeleteSelected() {
    if (selectedTaskIds.value.length === 0) return
    for (const taskId of selectedTaskIds.value) {
      await api.deleteTask(taskId, null)
    }
    selectedTaskIds.value = []
    if (selectedTask.value && !flattenGroups(groups.value).some((task) => task.id === selectedTask.value?.id)) {
      selectedTask.value = null
    }
    await fetchTasks()
  }

  async function reorderInGroup(taskIds: number[]) {
    await api.reorderTasks(taskIds, null)
    await fetchTasks()
  }

  async function deleteTask(taskId: number) {
    await api.deleteTask(taskId, null)
    if (selectedTask.value && String(selectedTask.value.id) === String(taskId)) {
      selectedTask.value = null
    }
    await fetchTasks()
  }

  async function createTaskType(name: string) {
    if (!isAdmin.value) return
    const normalized = name.trim()
    if (!normalized) return

    typeManageLoading.value = true
    typeManageError.value = null
    try {
      const created = await api.createTaskType({ name: normalized })
      taskTypes.value = [...taskTypes.value, created].sort(byNameAsc)
    } catch (err) {
      typeManageError.value = err instanceof Error ? err.message : 'Failed to create task type.'
      throw err
    } finally {
      typeManageLoading.value = false
    }
  }

  async function updateTaskType(typeId: number, name: string) {
    if (!isAdmin.value) return
    const normalized = name.trim()
    if (!normalized) return

    typeManageLoading.value = true
    typeManageError.value = null
    try {
      const updated = await api.updateTaskType(typeId, { name: normalized })
      taskTypes.value = taskTypes.value.map((item) => (item.id === typeId ? updated : item)).sort(byNameAsc)
      await fetchTasks()
    } catch (err) {
      typeManageError.value = err instanceof Error ? err.message : 'Failed to update task type.'
      throw err
    } finally {
      typeManageLoading.value = false
    }
  }

  async function deleteTaskType(typeId: number) {
    if (!isAdmin.value) return

    typeManageLoading.value = true
    typeManageError.value = null
    try {
      await api.deleteTaskType(typeId)
      taskTypes.value = taskTypes.value.filter((item) => item.id !== typeId)
      if (typeFilter.value === typeId) {
        typeFilter.value = null
      }
      await fetchTasks()
    } catch (err) {
      typeManageError.value = err instanceof Error ? err.message : 'Failed to delete task type.'
      throw err
    } finally {
      typeManageLoading.value = false
    }
  }

  async function createShop(name: string) {
    if (!isAdmin.value) return
    const normalized = name.trim()
    if (!normalized) return

    typeManageLoading.value = true
    typeManageError.value = null
    try {
      const created = await api.createShop({ name: normalized })
      shops.value = [...shops.value, created].sort(byNameAsc)
    } catch (err) {
      typeManageError.value = err instanceof Error ? err.message : 'Failed to create shop.'
      throw err
    } finally {
      typeManageLoading.value = false
    }
  }

  async function updateShop(shopId: number, name: string) {
    if (!isAdmin.value) return
    const normalized = name.trim()
    if (!normalized) return

    typeManageLoading.value = true
    typeManageError.value = null
    try {
      const updated = await api.updateShop(shopId, { name: normalized })
      shops.value = shops.value.map((item) => (item.id === shopId ? updated : item)).sort(byNameAsc)
      await fetchTasks()
    } catch (err) {
      typeManageError.value = err instanceof Error ? err.message : 'Failed to update shop.'
      throw err
    } finally {
      typeManageLoading.value = false
    }
  }

  async function deleteShop(shopId: number) {
    if (!isAdmin.value) return

    typeManageLoading.value = true
    typeManageError.value = null
    try {
      await api.deleteShop(shopId)
      shops.value = shops.value.filter((item) => item.id !== shopId)
      if (shopFilter.value === shopId) {
        shopFilter.value = null
      }
      await fetchTasks()
    } catch (err) {
      typeManageError.value = err instanceof Error ? err.message : 'Failed to delete shop.'
      throw err
    } finally {
      typeManageLoading.value = false
    }
  }

  async function addSubtask(taskId: number, content: string) {
    const current = selectedTask.value?.subtasks ?? []
    await api.createSubtask(taskId, { content, position: current.length + 1 }, null)
    await fetchTasks()
    selectedTask.value = flattenGroups(groups.value).find((task) => String(task.id) === String(taskId)) ?? null
  }

  async function updateSubtask(taskId: number, subtaskId: number, payload: Partial<Subtask>) {
    await api.updateSubtask(taskId, subtaskId, payload, null)
    await fetchTasks()
    selectedTask.value = flattenGroups(groups.value).find((task) => String(task.id) === String(taskId)) ?? null
  }

  async function deleteSubtask(taskId: number, subtaskId: number) {
    await api.deleteSubtask(taskId, subtaskId, null)
    await fetchTasks()
    selectedTask.value = flattenGroups(groups.value).find((task) => String(task.id) === String(taskId)) ?? null
  }

  return {
    users,
    shops,
    taskTypes,
    groups,
    view,
    viewCounts,
    assigneeId,
    shopFilter,
    typeFilter,
    selectedTaskIds,
    selectedTaskCount,
    hasBulkSelection,
    selectedTask,
    selectedTaskId,
    activeUser,
    isAdmin,
    canAssignTasks,
    canApproveTasks,
    typeManageLoading,
    typeManageError,
    loading,
    error,
    bootstrap,
    fetchTasks,
    setView,
    setAssignee,
    setShopFilter,
    setTypeFilter,
    selectTask,
    toggleTaskSelection,
    clearBulkSelection,
    createTask,
    updateTask,
    openTask,
    convertTask,
    toggleTaskDone,
    bulkUpdateSelected,
    bulkDeleteSelected,
    reorderInGroup,
    deleteTask,
    createTaskType,
    updateTaskType,
    deleteTaskType,
    createShop,
    updateShop,
    deleteShop,
    addSubtask,
    updateSubtask,
    deleteSubtask
  }
})
