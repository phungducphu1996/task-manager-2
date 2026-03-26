<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { Shop, TaskPayload, TaskView, User } from '../types'
import { analyzeQuickTaskInput, applyQuickAddSuggestion, type QuickAddSuggestion } from '../utils/quickAdd'

const props = defineProps<{
  users: User[]
  shops: Shop[]
  activeAssigneeId: string | null
  activeRole: User['role'] | null
  view: TaskView
}>()

const emit = defineEmits<{
  (e: 'create', payload: TaskPayload): void
}>()

const inputValue = ref('')
const focused = ref(false)
const activeSuggestionIndex = ref(0)
const inputRef = ref<HTMLInputElement | null>(null)
const isComposing = ref(false)
const isSubmitting = ref(false)
const lastSubmit = ref<{ fingerprint: string; at: number } | null>(null)

const analysis = computed(() =>
  analyzeQuickTaskInput(inputValue.value, {
    users: props.users,
    shops: props.shops,
    activeAssigneeId: props.activeAssigneeId,
    activeRole: props.activeRole,
    view: props.view
  })
)

watch(
  () => analysis.value.suggestions.map((item) => item.key).join('|'),
  () => {
    activeSuggestionIndex.value = 0
  }
)

function submit() {
  if (isSubmitting.value) return
  const raw = inputValue.value.trim()
  if (!raw) return
  const now = Date.now()
  if (
    lastSubmit.value &&
    lastSubmit.value.fingerprint === raw.toLowerCase() &&
    now - lastSubmit.value.at < 1200
  ) {
    return
  }

  if (!analysis.value.payload) return
  isSubmitting.value = true
  lastSubmit.value = { fingerprint: raw.toLowerCase(), at: now }
  emit('create', analysis.value.payload)
  inputValue.value = ''
  activeSuggestionIndex.value = 0
  nextTick(() => {
    inputRef.value?.focus()
    setTimeout(() => {
      isSubmitting.value = false
    }, 200)
  })
}

function applySuggestion(suggestion: QuickAddSuggestion) {
  inputValue.value = applyQuickAddSuggestion(inputValue.value, suggestion.token)
  activeSuggestionIndex.value = 0
  nextTick(() => inputRef.value?.focus())
}

function onKeydown(event: KeyboardEvent) {
  if (event.repeat) return
  if (event.isComposing || isComposing.value) return

  if (event.key === 'Enter') {
    event.preventDefault()
    submit()
    return
  }

  if (analysis.value.suggestions.length === 0) return

  if (event.key === 'ArrowDown') {
    event.preventDefault()
    activeSuggestionIndex.value = (activeSuggestionIndex.value + 1) % analysis.value.suggestions.length
    return
  }

  if (event.key === 'ArrowUp') {
    event.preventDefault()
    activeSuggestionIndex.value =
      (activeSuggestionIndex.value - 1 + analysis.value.suggestions.length) % analysis.value.suggestions.length
    return
  }

  if (event.key === 'Tab') {
    event.preventDefault()
    const selected = analysis.value.suggestions[activeSuggestionIndex.value]
    if (selected) applySuggestion(selected)
  }
}
</script>

<template>
  <div class="quick-composer">
    <div class="quick-input-wrap">
      <input
        ref="inputRef"
        v-model="inputValue"
        class="quick-composer-input"
        type="text"
        placeholder="Type task then Enter. Use #shop @assignee !priority due-date"
        @focus="focused = true"
        @blur="focused = false"
        @keydown="onKeydown"
        @compositionstart="isComposing = true"
        @compositionend="isComposing = false"
      />

      <div v-if="focused && analysis.suggestions.length > 0" class="quick-suggestion-menu">
        <button
          v-for="(suggestion, index) in analysis.suggestions"
          :key="suggestion.key"
          class="quick-suggestion-item"
          :class="{ active: index === activeSuggestionIndex }"
          @mousedown.prevent="applySuggestion(suggestion)"
        >
          {{ suggestion.label }}
        </button>
      </div>
    </div>

    <div v-if="inputValue.trim()" class="quick-preview-row">
      <span v-for="chip in analysis.chips" :key="chip.key" class="quick-preview-chip" :class="{ default: chip.isDefault }">
        {{ chip.text }}
      </span>
    </div>
  </div>
</template>
