<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/authStore'
import { useTaskStore } from '../stores/taskStore'
import { api } from '../services/api'
import type { ReminderRule, ReminderRulePayload, ReminderRuleType, ReminderTickResult } from '../types'

const router = useRouter()
const auth = useAuthStore()
const store = useTaskStore()

const bootstrapping = ref(true)
const newTypeName = ref('')
const newShopName = ref('')
const typeDrafts = ref<Record<number, string>>({})
const shopDrafts = ref<Record<number, string>>({})
const reminders = ref<ReminderRule[]>([])
const reminderLoading = ref(false)
const reminderError = ref<string | null>(null)
const tickLoading = ref(false)
const tickResult = ref<ReminderTickResult | null>(null)
const newReminderType = ref<ReminderRuleType>('daily_group_digest')
const newReminderName = ref('')
const newReminderTime = ref('08:00')
const newReminderInterval = ref(60)
const newReminderTaskId = ref('')
const newReminderTargetMode = ref<'default' | 'group' | 'user'>('default')
const newReminderGroupId = ref('')
const newReminderUserId = ref('')

const reminderTypeOptions: Array<{ value: ReminderRuleType; label: string; hint: string }> = [
  {
    value: 'daily_group_digest',
    label: '8AM group digest',
    hint: 'Tổng quan group: today, overdue, review, workload.'
  },
  {
    value: 'daily_member_checkin',
    label: '9AM member check-in',
    hint: 'Nhắn từng thành viên việc của họ và hỏi tình hình.'
  },
  {
    value: 'daily_strategy',
    label: 'Daily strategy',
    hint: 'Gửi admin đề xuất hướng xử lý trong ngày.'
  },
  {
    value: 'task_nudge',
    label: 'Task nudge',
    hint: 'Nhắc một task theo chu kỳ đến khi review/ready/done.'
  }
]

const selectedReminderOption = computed(
  () => reminderTypeOptions.find((item) => item.value === newReminderType.value) ?? reminderTypeOptions[0]
)

const activeReminders = computed(() => reminders.value.filter((item) => item.enabled))
const disabledReminders = computed(() => reminders.value.filter((item) => !item.enabled))

watch(
  () => store.taskTypes,
  (next) => {
    const draftMap: Record<number, string> = {}
    for (const item of next) {
      draftMap[item.id] = typeDrafts.value[item.id] ?? item.name
    }
    typeDrafts.value = draftMap
  },
  { immediate: true, deep: true }
)

watch(
  () => store.shops,
  (next) => {
    const draftMap: Record<number, string> = {}
    for (const item of next) {
      draftMap[item.id] = shopDrafts.value[item.id] ?? item.name
    }
    shopDrafts.value = draftMap
  },
  { immediate: true, deep: true }
)

onMounted(async () => {
  await auth.bootstrap()
  if (!auth.user) {
    await router.replace('/login')
    return
  }

  await store.bootstrap(auth.user.id, auth.user.role)
  if (!store.isAdmin) {
    await router.replace('/today')
    return
  }
  await loadReminders()
  bootstrapping.value = false
})

async function goBack() {
  await router.push('/today')
}

async function createType() {
  const name = newTypeName.value.trim()
  if (!name) return
  try {
    await store.createTaskType(name)
    newTypeName.value = ''
  } catch {
    // Error message is shown by store.typeManageError.
  }
}

async function saveType(typeId: number) {
  const name = (typeDrafts.value[typeId] ?? '').trim()
  if (!name) return
  try {
    await store.updateTaskType(typeId, name)
  } catch {
    // Error message is shown by store.typeManageError.
  }
}

async function removeType(typeId: number) {
  const target = store.taskTypes.find((item) => item.id === typeId)
  const accepted = window.confirm(`Delete task type "${target?.name ?? typeId}"?`)
  if (!accepted) return
  try {
    await store.deleteTaskType(typeId)
  } catch {
    // Error message is shown by store.typeManageError.
  }
}

async function createShop() {
  const name = newShopName.value.trim()
  if (!name) return
  try {
    await store.createShop(name)
    newShopName.value = ''
  } catch {
    // Error message is shown by store.typeManageError.
  }
}

async function saveShop(shopId: number) {
  const name = (shopDrafts.value[shopId] ?? '').trim()
  if (!name) return
  try {
    await store.updateShop(shopId, name)
  } catch {
    // Error message is shown by store.typeManageError.
  }
}

async function removeShop(shopId: number) {
  const target = store.shops.find((item) => item.id === shopId)
  const accepted = window.confirm(`Delete shop "${target?.name ?? shopId}"?`)
  if (!accepted) return
  try {
    await store.deleteShop(shopId)
  } catch {
    // Error message is shown by store.typeManageError.
  }
}

function normalizeTime(value: string): string | null {
  if (!value) return null
  return value.length === 5 ? `${value}:00` : value
}

function reminderTypeLabel(type: ReminderRuleType): string {
  return reminderTypeOptions.find((item) => item.value === type)?.label ?? type
}

function reminderSchedule(rule: ReminderRule): string {
  if (rule.schedule_type === 'interval') {
    return `every ${rule.interval_minutes ?? '?'} min`
  }
  return rule.schedule_time ? `daily ${rule.schedule_time.slice(0, 5)}` : 'daily'
}

function reminderTarget(rule: ReminderRule): string {
  if (rule.task_id) return `task #${rule.task_id}`
  if (rule.user_id) {
    const user = store.users.find((item) => item.id === rule.user_id)
    return user ? user.name : `user ${rule.user_id}`
  }
  if (rule.target_channel === 'group') return rule.target_id ? `group ${rule.target_id}` : 'group'
  if (rule.target_channel === 'user') return rule.target_id ? `zalo ${rule.target_id}` : 'user'
  if (rule.rule_type === 'daily_member_checkin') return 'all active members'
  if (rule.rule_type === 'daily_strategy') return 'admins'
  return 'default group'
}

async function loadReminders() {
  reminderLoading.value = true
  reminderError.value = null
  try {
    reminders.value = await api.getReminders(auth.user?.id ?? null)
  } catch (error) {
    reminderError.value = error instanceof Error ? error.message : 'Failed to load reminders.'
  } finally {
    reminderLoading.value = false
  }
}

async function createReminder() {
  reminderError.value = null
  const taskId = Number(newReminderTaskId.value)
  if (newReminderType.value === 'task_nudge' && (!Number.isInteger(taskId) || taskId <= 0)) {
    reminderError.value = 'Task nudge cần nhập Task ID hợp lệ nha anh.'
    return
  }
  if (newReminderTargetMode.value === 'group' && !newReminderGroupId.value.trim()) {
    reminderError.value = 'Specific group cần Group ID. Nếu muốn dùng ZALO_GROUP_ID mặc định thì chọn Smart default nha anh.'
    return
  }
  if (newReminderTargetMode.value === 'user' && !newReminderUserId.value) {
    reminderError.value = 'Specific user cần chọn một user nha anh.'
    return
  }

  const payload: ReminderRulePayload = {
    name: newReminderName.value.trim() || selectedReminderOption.value.label,
    rule_type: newReminderType.value,
    schedule_type: newReminderType.value === 'task_nudge' ? 'interval' : 'daily',
    schedule_time: newReminderType.value === 'task_nudge' ? null : normalizeTime(newReminderTime.value),
    interval_minutes: newReminderType.value === 'task_nudge' ? newReminderInterval.value : null,
    task_id: newReminderType.value === 'task_nudge' ? taskId : null
  }

  if (newReminderTargetMode.value === 'group') {
    payload.target_channel = 'group'
    payload.target_id = newReminderGroupId.value.trim() || null
  }
  if (newReminderTargetMode.value === 'user') {
    payload.user_id = newReminderUserId.value || null
  }

  reminderLoading.value = true
  try {
    await api.createReminder(payload, auth.user?.id ?? null)
    newReminderName.value = ''
    newReminderTaskId.value = ''
    await loadReminders()
  } catch (error) {
    reminderError.value = error instanceof Error ? error.message : 'Failed to create reminder.'
  } finally {
    reminderLoading.value = false
  }
}

async function setReminderEnabled(rule: ReminderRule, enabled: boolean) {
  reminderError.value = null
  reminderLoading.value = true
  try {
    await api.updateReminder(rule.id, { enabled }, auth.user?.id ?? null)
    await loadReminders()
  } catch (error) {
    reminderError.value = error instanceof Error ? error.message : 'Failed to update reminder.'
  } finally {
    reminderLoading.value = false
  }
}

async function disableReminder(rule: ReminderRule) {
  reminderError.value = null
  reminderLoading.value = true
  try {
    await api.deleteReminder(rule.id, auth.user?.id ?? null)
    await loadReminders()
  } catch (error) {
    reminderError.value = error instanceof Error ? error.message : 'Failed to disable reminder.'
  } finally {
    reminderLoading.value = false
  }
}

async function runTickNow() {
  tickLoading.value = true
  reminderError.value = null
  tickResult.value = null
  try {
    tickResult.value = await api.runReminderTick(auth.user?.id ?? null)
    await loadReminders()
  } catch (error) {
    reminderError.value = error instanceof Error ? error.message : 'Failed to run reminder tick.'
  } finally {
    tickLoading.value = false
  }
}
</script>

<template>
  <main class="manage-page">
    <header class="manage-header">
      <div>
        <h1>Manage Catalog</h1>
        <p>Edit names or add/remove shops and task types.</p>
      </div>
      <button class="ghost-btn" type="button" @click="goBack">Back to Tasks</button>
    </header>

    <section v-if="bootstrapping" class="loading-state">Loading management data...</section>

    <template v-else>
      <p v-if="store.typeManageError" class="error-message">{{ store.typeManageError }}</p>

      <div class="manage-grid">
        <section class="manage-card reminder-lab-card">
          <div class="manage-card-head">
            <div>
              <h2>Reminder Lab</h2>
              <p>Tạo rule test nhanh, rồi bấm tick để thử gửi Zalo nếu rule đang tới giờ.</p>
            </div>
            <button class="ghost-btn" type="button" :disabled="tickLoading || reminderLoading" @click="runTickNow">
              {{ tickLoading ? 'Running...' : 'Run tick now' }}
            </button>
          </div>

          <p v-if="reminderError" class="error-message">{{ reminderError }}</p>

          <div class="reminder-create-panel">
            <label>
              Type
              <select v-model="newReminderType" :disabled="reminderLoading">
                <option v-for="option in reminderTypeOptions" :key="option.value" :value="option.value">
                  {{ option.label }}
                </option>
              </select>
            </label>

            <label>
              Name
              <input v-model="newReminderName" type="text" :placeholder="selectedReminderOption.label" />
            </label>

            <label v-if="newReminderType !== 'task_nudge'">
              Time
              <input v-model="newReminderTime" type="time" />
            </label>

            <label v-else>
              Every minutes
              <input v-model.number="newReminderInterval" type="number" min="1" />
            </label>

            <label v-if="newReminderType === 'task_nudge'">
              Task ID
              <input v-model="newReminderTaskId" type="number" min="1" placeholder="73" />
            </label>

            <label>
              Target
              <select v-model="newReminderTargetMode" :disabled="reminderLoading">
                <option value="default">Smart default</option>
                <option value="group">Specific group</option>
                <option value="user">Specific user</option>
              </select>
            </label>

            <label v-if="newReminderTargetMode === 'group'">
              Group ID
              <input v-model="newReminderGroupId" type="text" placeholder="ZALO_GROUP_ID" />
            </label>

            <label v-if="newReminderTargetMode === 'user'">
              User
              <select v-model="newReminderUserId">
                <option value="">Choose user</option>
                <option v-for="user in store.users" :key="user.id" :value="user.id">{{ user.name }}</option>
              </select>
            </label>
          </div>

          <div class="reminder-create-footer">
            <p>{{ selectedReminderOption.hint }}</p>
            <button class="primary-btn" type="button" :disabled="reminderLoading" @click="createReminder">
              Create reminder
            </button>
          </div>

          <div v-if="tickResult" class="tick-result">
            <strong>Last tick</strong>
            <span>rules checked {{ tickResult.rules_checked }}</span>
            <span>runs +{{ tickResult.runs_created }} / dedupe {{ tickResult.runs_deduped }}</span>
            <span>escalations +{{ tickResult.escalations_created }}</span>
            <span>sent {{ tickResult.dispatch.sent }}, pending {{ tickResult.dispatch.pending }}</span>
          </div>

          <div class="reminder-list-wrap">
            <div class="reminder-list-title">
              <strong>Active rules</strong>
              <span>{{ activeReminders.length }} active · {{ disabledReminders.length }} paused</span>
            </div>

            <ul class="reminder-list">
              <li v-for="rule in reminders" :key="rule.id" :class="{ paused: !rule.enabled }">
                <div>
                  <strong>{{ rule.name }}</strong>
                  <span>{{ reminderTypeLabel(rule.rule_type) }} · {{ reminderSchedule(rule) }} · {{ reminderTarget(rule) }}</span>
                </div>
                <span class="reminder-state" :class="{ off: !rule.enabled }">{{ rule.enabled ? 'On' : 'Off' }}</span>
                <button
                  v-if="rule.enabled"
                  class="ghost-btn danger"
                  type="button"
                  :disabled="reminderLoading"
                  @click="disableReminder(rule)"
                >
                  Pause
                </button>
                <button
                  v-else
                  class="ghost-btn"
                  type="button"
                  :disabled="reminderLoading"
                  @click="setReminderEnabled(rule, true)"
                >
                  Resume
                </button>
              </li>
            </ul>

            <p v-if="!reminderLoading && reminders.length === 0" class="empty-hint">
              Chưa có reminder nào. Tạo thử một rule 8AM hoặc task nudge là test được liền.
            </p>
          </div>
        </section>

        <section class="manage-card">
          <h2>Task Types</h2>

          <div class="manage-create">
            <input
              v-model="newTypeName"
              type="text"
              placeholder="Add task type"
              :disabled="store.typeManageLoading"
              @keydown.enter.prevent="createType"
            />
            <button class="primary-btn" type="button" :disabled="store.typeManageLoading" @click="createType">
              Add
            </button>
          </div>

          <ul class="manage-list">
            <li v-for="item in store.taskTypes" :key="item.id">
              <input v-model="typeDrafts[item.id]" type="text" :disabled="store.typeManageLoading" />
              <button class="ghost-btn" type="button" :disabled="store.typeManageLoading" @click="saveType(item.id)">
                Save
              </button>
              <button class="ghost-btn danger" type="button" :disabled="store.typeManageLoading" @click="removeType(item.id)">
                Remove
              </button>
            </li>
          </ul>
        </section>

        <section class="manage-card">
          <h2>Shops</h2>

          <div class="manage-create">
            <input
              v-model="newShopName"
              type="text"
              placeholder="Add shop"
              :disabled="store.typeManageLoading"
              @keydown.enter.prevent="createShop"
            />
            <button class="primary-btn" type="button" :disabled="store.typeManageLoading" @click="createShop">
              Add
            </button>
          </div>

          <ul class="manage-list">
            <li v-for="item in store.shops" :key="item.id">
              <input v-model="shopDrafts[item.id]" type="text" :disabled="store.typeManageLoading" />
              <button class="ghost-btn" type="button" :disabled="store.typeManageLoading" @click="saveShop(item.id)">
                Save
              </button>
              <button class="ghost-btn danger" type="button" :disabled="store.typeManageLoading" @click="removeShop(item.id)">
                Remove
              </button>
            </li>
          </ul>
        </section>
      </div>
    </template>
  </main>
</template>
