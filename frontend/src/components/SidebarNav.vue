<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import type { Shop, TaskType, TaskView, User } from '../types'

const props = defineProps<{
  currentView: TaskView
  users: User[]
  shops: Shop[]
  taskTypes: TaskType[]
  viewCounts?: Partial<Record<TaskView, number>>
  assigneeId: string | null
  activeRole: User['role'] | null
  lockAssignee: boolean
  shopFilter: number | null
  typeFilter: number | null
}>()

const emit = defineEmits<{
  (e: 'change-view', value: TaskView): void
  (e: 'change-assignee', value: string | null): void
  (e: 'change-shop', value: number | null): void
  (e: 'change-type', value: number | null): void
  (e: 'open-manage'): void
  (e: 'logout'): void
}>()

type NavIcon = 'inbox' | 'today' | 'upcoming' | 'anytime' | 'logbook' | 'review'

const navItems: { key: TaskView; label: string; icon: NavIcon }[] = [
  { key: 'inbox', label: 'Inbox', icon: 'inbox' },
  { key: 'today', label: 'Today', icon: 'today' },
  { key: 'upcoming', label: 'Upcoming', icon: 'upcoming' },
  { key: 'anytime', label: 'Anytime', icon: 'anytime' },
  { key: 'logbook', label: 'Logbook', icon: 'logbook' },
  { key: 'review', label: 'Review Queue', icon: 'review' }
]

function getViewCount(viewKey: TaskView): number {
  return props.viewCounts?.[viewKey] ?? 0
}

function toNullableInt(value: string): number | null {
  if (!value) return null
  return Number(value)
}

interface AssigneeOption {
  id: string | null
  name: string
  avatarUrl: string | null
}

const assigneeMenuRef = ref<HTMLElement | null>(null)
const assigneeMenuOpen = ref(false)

const assigneeOptions = computed<AssigneeOption[]>(() => {
  const options: AssigneeOption[] = props.users.map((user) => ({
    id: user.id,
    name: user.name,
    avatarUrl: user.avatar_url ?? null
  }))

  if (props.activeRole === 'admin') {
    options.unshift({
      id: null,
      name: 'All members',
      avatarUrl: null
    })
  }

  return options
})

const selectedAssignee = computed<AssigneeOption>(() => {
  const matched = assigneeOptions.value.find((item) => item.id === props.assigneeId)
  if (matched) return matched

  if (props.activeRole === 'admin' && props.assigneeId === null) {
    return {
      id: null,
      name: 'All members',
      avatarUrl: null
    }
  }

  return assigneeOptions.value[0] ?? { id: null, name: 'Member', avatarUrl: null }
})

function avatarInitial(name: string): string {
  const trimmed = name.trim()
  if (!trimmed) return '?'
  return trimmed.charAt(0).toUpperCase()
}

function toggleAssigneeMenu() {
  if (props.lockAssignee) return
  assigneeMenuOpen.value = !assigneeMenuOpen.value
}

function selectAssignee(value: string | null) {
  emit('change-assignee', value)
  assigneeMenuOpen.value = false
}

function handleOutsideClick(event: MouseEvent) {
  const target = event.target as Node | null
  if (!target || !assigneeMenuRef.value) return
  if (assigneeMenuRef.value.contains(target)) return
  assigneeMenuOpen.value = false
}

onMounted(() => {
  if (typeof window === 'undefined') return
  window.addEventListener('mousedown', handleOutsideClick)
})

onBeforeUnmount(() => {
  if (typeof window === 'undefined') return
  window.removeEventListener('mousedown', handleOutsideClick)
})
</script>

<template>
  <aside class="sidebar">
    <div class="brand">
      <h1>Team Task</h1>
      <p>Execution First</p>
      <button class="logout-btn" @click="emit('logout')">Log out</button>
    </div>

    <nav class="nav-list" aria-label="Views">
      <button
        v-for="item in navItems"
        :key="item.key"
        v-show="item.key !== 'review' || props.activeRole === 'admin'"
        class="nav-item"
        :class="{ active: item.key === props.currentView }"
        :data-testid="`nav-item-${item.key}`"
        @click="emit('change-view', item.key)"
      >
        <span class="nav-item-content">
          <span class="nav-item-main">
            <span class="nav-icon" :class="`icon-${item.icon}`" :data-testid="`nav-icon-${item.key}`" aria-hidden="true">
              <svg v-if="item.icon === 'inbox'" viewBox="0 0 20 20" fill="none">
                <path d="M2.5 6.25a2 2 0 0 1 2-2h11a2 2 0 0 1 2 2v7.5a2 2 0 0 1-2 2h-11a2 2 0 0 1-2-2z" stroke="currentColor" stroke-width="1.8"/>
                <path d="M6.5 9.75h2l1.2 1.8h.6l1.2-1.8h2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
              <svg v-else-if="item.icon === 'today'" viewBox="0 0 20 20" fill="none">
                <path d="m10 2.2 2.18 4.42 4.88.71-3.53 3.45.83 4.87L10 13.34 5.64 15.65l.83-4.87L2.94 7.33l4.88-.71z" fill="currentColor"/>
              </svg>
              <svg v-else-if="item.icon === 'upcoming'" viewBox="0 0 20 20" fill="none">
                <rect x="3" y="4" width="14" height="13" rx="2.5" stroke="currentColor" stroke-width="1.8"/>
                <path d="M3 7.5h14M6.2 2.8v2.8M13.8 2.8v2.8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
                <circle cx="7.1" cy="11.6" r="1" fill="currentColor"/>
              </svg>
              <svg v-else-if="item.icon === 'anytime'" viewBox="0 0 20 20" fill="none">
                <path d="m10 3 6.4 3.2L10 9.4 3.6 6.2z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>
                <path d="m3.6 9.3 6.4 3.2 6.4-3.2M3.6 12.4l6.4 3.2 6.4-3.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
              <svg v-else-if="item.icon === 'logbook'" viewBox="0 0 20 20" fill="none">
                <rect x="4" y="2.8" width="12" height="14.4" rx="2.2" stroke="currentColor" stroke-width="1.8"/>
                <path d="m7.2 10.2 1.8 1.9 3.8-4.2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
              <svg v-else viewBox="0 0 20 20" fill="none">
                <circle cx="10" cy="10" r="7" stroke="currentColor" stroke-width="1.8"/>
                <path d="m7.2 10.2 1.8 1.9 3.8-4.2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </span>
            <span class="nav-label">{{ item.label }}</span>
          </span>
          <span
            v-if="getViewCount(item.key) > 0"
            class="nav-count"
            :class="{ 'is-today': item.key === 'today' }"
            :data-testid="`nav-count-${item.key}`"
          >
            {{ getViewCount(item.key) }}
          </span>
        </span>
      </button>
    </nav>

    <button
      v-if="props.activeRole === 'admin'"
      class="nav-item manage-entry"
      type="button"
      @click="emit('open-manage')"
    >
      Manage
    </button>

    <section class="sidebar-section">
      <label id="assignee-label">Active Member</label>
      <div ref="assigneeMenuRef" class="assignee-picker">
        <button
          class="assignee-trigger"
          type="button"
          :disabled="props.lockAssignee"
          aria-haspopup="listbox"
          :aria-expanded="assigneeMenuOpen ? 'true' : 'false'"
          aria-labelledby="assignee-label"
          @click="toggleAssigneeMenu"
        >
          <span class="assignee-avatar">
            <img
              v-if="selectedAssignee.avatarUrl"
              :src="selectedAssignee.avatarUrl"
              :alt="selectedAssignee.name"
            />
            <span v-else>{{ avatarInitial(selectedAssignee.name) }}</span>
          </span>
          <span class="assignee-name">{{ selectedAssignee.name }}</span>
          <span class="assignee-caret">⌄</span>
        </button>

        <ul
          v-if="assigneeMenuOpen && !props.lockAssignee"
          class="assignee-menu"
          role="listbox"
          aria-labelledby="assignee-label"
        >
          <li v-for="option in assigneeOptions" :key="option.id ?? 'all-members'">
            <button
              class="assignee-option"
              :class="{ selected: option.id === selectedAssignee.id }"
              type="button"
              role="option"
              :aria-selected="option.id === selectedAssignee.id ? 'true' : 'false'"
              @click="selectAssignee(option.id)"
            >
              <span class="assignee-avatar">
                <img v-if="option.avatarUrl" :src="option.avatarUrl" :alt="option.name" />
                <span v-else>{{ avatarInitial(option.name) }}</span>
              </span>
              <span>{{ option.name }}</span>
            </button>
          </li>
        </ul>
      </div>
    </section>

    <section class="sidebar-section">
      <label for="shop-filter">Shop</label>
      <select
        id="shop-filter"
        :value="props.shopFilter ?? ''"
        @change="emit('change-shop', toNullableInt(($event.target as HTMLSelectElement).value))"
      >
        <option value="">All shops</option>
        <option v-for="shop in props.shops" :key="shop.id" :value="shop.id">{{ shop.name }}</option>
      </select>
    </section>

    <section class="sidebar-section">
      <label for="type-filter">Task Type</label>
      <select
        id="type-filter"
        :value="props.typeFilter ?? ''"
        @change="emit('change-type', toNullableInt(($event.target as HTMLSelectElement).value))"
      >
        <option value="">All types</option>
        <option v-for="taskType in props.taskTypes" :key="taskType.id" :value="taskType.id">{{ taskType.name }}</option>
      </select>
    </section>
  </aside>
</template>
