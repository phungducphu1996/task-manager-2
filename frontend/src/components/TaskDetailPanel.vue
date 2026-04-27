<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api } from '../services/api'
import type {
  Shop,
  Subtask,
  Task,
  TaskAttachment,
  TaskComment,
  TaskPayload,
  TaskPriority,
  TaskStatus,
  TaskType,
  User
} from '../types'

const MAX_ATTACHMENT_MB = 50
const MAX_ATTACHMENT_BYTES = MAX_ATTACHMENT_MB * 1024 * 1024
const SHOW_COMMENTS = false
const SHOW_CHECKLIST = false
const SHOW_NOTES = false

const props = defineProps<{
  task: Task | null
  users: User[]
  currentUserId: string | null
  isAdmin: boolean
  shops: Shop[]
  taskTypes: TaskType[]
}>()

const emit = defineEmits<{
  (e: 'update-task', taskId: number, payload: Partial<TaskPayload>): void
  (e: 'delete-task', taskId: number): void
  (e: 'add-subtask', taskId: number, content: string): void
  (e: 'update-subtask', taskId: number, subtaskId: number, payload: Partial<Subtask>): void
  (e: 'delete-subtask', taskId: number, subtaskId: number): void
  (e: 'open-task', taskId: number): void
  (e: 'convert-task', taskId: number, targetTypeId: number, done: (errorMessage?: string) => void): void
}>()

const form = reactive({
  title: '',
  description: '',
  status: 'todo' as TaskStatus,
  assigned_to: null as string | null,
  shop_id: null as number | null,
  type_id: null as number | null,
  due_date: '',
  priority: 'medium' as TaskPriority,
  notes: ''
})

const newSubtask = ref('')
const saveState = ref<'idle' | 'saving' | 'saved'>('idle')
const isHydrating = ref(false)
const subtaskDrafts = reactive<Record<number, string>>({})
const lastSavedSerialized = ref('')
let saveTimer: ReturnType<typeof setTimeout> | null = null
let saveStateTimer: ReturnType<typeof setTimeout> | null = null
const subtaskTimers = new Map<number, ReturnType<typeof setTimeout>>()
let sideLoadSeq = 0

const comments = ref<TaskComment[]>([])
const attachments = ref<TaskAttachment[]>([])
const commentDraft = ref('')
const commentInputRef = ref<HTMLTextAreaElement | null>(null)
const attachmentInputRef = ref<HTMLInputElement | null>(null)
const linkUrlInputRef = ref<HTMLInputElement | null>(null)
const showLinkComposer = ref(false)
const linkUrlDraft = ref('')
const linkNameDraft = ref('')
const isAddingLink = ref(false)
const mentionState = ref<{ start: number; end: number; query: string } | null>(null)
const isAttachmentDragOver = ref(false)
const attachmentError = ref<string | null>(null)
const commentError = ref<string | null>(null)
const loadingSecondary = ref(false)
const isCommentComposing = ref(false)
const isSendingComment = ref(false)
const isUploadingAttachment = ref(false)
const lastCommentSignature = ref('')
const lastCommentSentAt = ref(0)
const statusOptions: TaskStatus[] = ['todo', 'doing', 'review', 'ready', 'done']
const convertOpen = ref(false)
const convertTargetTypeId = ref<number | null>(null)
const convertLoading = ref(false)
const convertError = ref<string | null>(null)
const convertNotice = ref<string | null>(null)

const currentUser = computed(() => props.users.find((item) => item.id === props.currentUserId) ?? null)
const approvalState = computed(() => {
  if (form.status === 'review') {
    return { label: 'Pending admin approval', tone: 'pending' as const }
  }
  if (form.status === 'ready') {
    return { label: 'Approved by admin', tone: 'approved' as const }
  }
  return null
})

const canConvert = computed(() => {
  const task = props.task
  if (!task) return false
  if (task.status !== 'ready' && task.status !== 'done') return false
  if (props.isAdmin) return true
  return Boolean(props.currentUserId && task.assigned_to === props.currentUserId)
})

interface AssigneeOption {
  id: string | null
  name: string
  avatarUrl: string | null
}

const statusPickerRef = ref<HTMLElement | null>(null)
const statusMenuOpen = ref(false)
const assigneePickerRef = ref<HTMLElement | null>(null)
const assigneeMenuOpen = ref(false)

const assigneeOptions = computed<AssigneeOption[]>(() => [
  { id: null, name: 'None', avatarUrl: null },
  ...props.users.map((user) => ({
    id: user.id,
    name: user.name,
    avatarUrl: user.avatar_url ?? null
  }))
])

const selectedAssigneeOption = computed<AssigneeOption>(() => {
  return assigneeOptions.value.find((item) => item.id === form.assigned_to) ?? assigneeOptions.value[0]
})

const mentionSuggestions = computed(() => {
  if (!mentionState.value) return []
  const query = mentionState.value.query.toLowerCase()
  return props.users
    .filter((user) => tokenAlias(user.name).toLowerCase().includes(query))
    .slice(0, 6)
})

function normalizeTitleTag(raw: string): string {
  const trimmed = raw.trim()
  if (!trimmed) return ''
  const stripped = trimmed
    .replace(/^[^\p{L}\p{N}]+/gu, '')
    .replace(/[^\p{L}\p{N}]+$/gu, '')
    .trim()
  return stripped || trimmed
}

function lowerTag(value: string): string {
  return normalizeTitleTag(value).toLocaleLowerCase()
}

function tagEquals(left: string, right: string): boolean {
  return lowerTag(left) === lowerTag(right)
}

function splitTitleTags(rawTitle: string): { tags: string[]; body: string } {
  const match = rawTitle.match(/^\s*(?:\[[^\]]+\]\s*)+/u)
  if (!match) {
    return { tags: [], body: rawTitle.trim() }
  }

  const tagTokens = match[0].match(/\[[^\]]+\]/g) ?? []
  const tags = tagTokens
    .map((item) => normalizeTitleTag(item.slice(1, -1)))
    .filter(Boolean)
  const body = rawTitle.slice(match[0].length).trim()
  return { tags, body }
}

function managedTagUniverse(): Set<string> {
  const known = new Set<string>()
  props.taskTypes.forEach((item) => {
    const normalized = lowerTag(item.name)
    if (normalized) known.add(normalized)
  })
  props.shops.forEach((item) => {
    const normalized = lowerTag(item.name)
    if (normalized) known.add(normalized)
  })
  return known
}

function syncManagedTitleTags() {
  const currentTitle = form.title.trim()
  if (!currentTitle) return

  const selectedType = props.taskTypes.find((item) => item.id === form.type_id)?.name ?? null
  const selectedShop = props.shops.find((item) => item.id === form.shop_id)?.name ?? null
  const managedWantedRaw = [selectedType, selectedShop]
    .map((item) => normalizeTitleTag(item ?? ''))
    .filter(Boolean)

  const managedWanted: string[] = []
  for (const item of managedWantedRaw) {
    if (!managedWanted.some((existing) => tagEquals(existing, item))) {
      managedWanted.push(item)
    }
  }

  const knownManaged = managedTagUniverse()
  const { tags, body } = splitTitleTags(currentTitle)
  const preservedManual = tags.filter((tag) => !knownManaged.has(lowerTag(tag)))
  const finalTags = [...managedWanted, ...preservedManual]

  const rebuilt = `${finalTags.map((tag) => `[${tag}]`).join(' ')}${body ? ` ${body}` : ''}`.trim()
  form.title = rebuilt || body
}

function onTypeChanged() {
  syncManagedTitleTags()
}

function onShopChanged() {
  syncManagedTitleTags()
}

function dedupeComments(list: TaskComment[]): TaskComment[] {
  if (list.length <= 1) return list

  const kept: TaskComment[] = []
  for (const item of list) {
    const currentTs = Number(new Date(item.created_at))
    const isDuplicate = kept.some((existing) => {
      const existingTs = Number(new Date(existing.created_at))
      return (
        existing.author_id === item.author_id &&
        existing.content.trim() === item.content.trim() &&
        Math.abs(existingTs - currentTs) < 1500
      )
    })

    if (!isDuplicate) kept.push(item)
  }

  return kept
}

async function loadTaskSideData(taskId: number): Promise<void> {
  if (!props.currentUserId) {
    comments.value = []
    attachments.value = []
    loadingSecondary.value = false
    return
  }

  const currentSeq = ++sideLoadSeq
  loadingSecondary.value = true
  attachmentError.value = null

  try {
    const fetchedAttachments = await api.getTaskAttachments(taskId, props.currentUserId)
    if (currentSeq !== sideLoadSeq) return
    attachments.value = fetchedAttachments
  } catch (error) {
    if (currentSeq !== sideLoadSeq) return
    const message = error instanceof Error ? error.message : 'Failed to load attachments.'
    attachmentError.value = message
  } finally {
    if (currentSeq !== sideLoadSeq) return
    loadingSecondary.value = false
  }
}

function payloadFromForm(): Partial<TaskPayload> {
  return {
    title: form.title,
    description: form.description || null,
    status: form.status,
    assigned_to: form.assigned_to,
    shop_id: form.shop_id,
    type_id: form.type_id,
    scheduled_date: null,
    due_date: form.due_date || null,
    priority: form.priority,
    notes: form.notes || null,
    is_someday: false
  }
}

function normalizePriorityForUi(priority: TaskPriority): TaskPriority {
  return priority === 'urgent' ? 'high' : priority
}

function payloadFromTask(task: Task): Partial<TaskPayload> {
  return {
    title: task.title,
    description: task.description ?? null,
    status: task.status,
    assigned_to: task.assigned_to,
    shop_id: task.shop_id,
    type_id: task.type_id,
    scheduled_date: null,
    due_date: task.due_date ?? null,
    priority: task.priority,
    notes: task.notes ?? null,
    is_someday: false
  }
}

function clearSaveTimer() {
  if (!saveTimer) return
  clearTimeout(saveTimer)
  saveTimer = null
}

function clearSaveStateTimer() {
  if (!saveStateTimer) return
  clearTimeout(saveStateTimer)
  saveStateTimer = null
}

function clearSubtaskTimers() {
  for (const timer of subtaskTimers.values()) {
    clearTimeout(timer)
  }
  subtaskTimers.clear()
}

function setSavedState() {
  saveState.value = 'saved'
  clearSaveStateTimer()
  saveStateTimer = setTimeout(() => {
    saveState.value = 'idle'
  }, 1200)
}

function markSaving() {
  saveState.value = 'saving'
  clearSaveStateTimer()
}

function scheduleTaskAutoSave() {
  if (!props.task || isHydrating.value) return

  const payload = payloadFromForm()
  const serialized = JSON.stringify(payload)
  if (serialized === lastSavedSerialized.value) return

  markSaving()
  clearSaveTimer()
  saveTimer = setTimeout(() => {
    if (!props.task) return
    emit('update-task', props.task.id, payload)
    lastSavedSerialized.value = serialized
    setSavedState()
  }, 420)
}

function scheduleSubtaskContentSave(subtask: Subtask) {
  if (!props.task) return
  const value = (subtaskDrafts[subtask.id] ?? '').trim()
  if (!value || value === subtask.content) return

  markSaving()
  const existingTimer = subtaskTimers.get(subtask.id)
  if (existingTimer) clearTimeout(existingTimer)

  const timer = setTimeout(() => {
    if (!props.task) return
    emit('update-subtask', props.task.id, subtask.id, { content: value })
    setSavedState()
  }, 320)

  subtaskTimers.set(subtask.id, timer)
}

function onSubtaskBlur(subtask: Subtask) {
  const value = (subtaskDrafts[subtask.id] ?? '').trim()
  if (!value) {
    subtaskDrafts[subtask.id] = subtask.content
    return
  }
  scheduleSubtaskContentSave(subtask)
}

function toggleSubtask(subtask: Subtask) {
  if (!props.task) return
  markSaving()
  emit('update-subtask', props.task.id, subtask.id, { is_done: !subtask.is_done })
  setSavedState()
}

function addSubtask() {
  if (!props.task || !newSubtask.value.trim()) return
  markSaving()
  emit('add-subtask', props.task.id, newSubtask.value.trim())
  newSubtask.value = ''
  setSavedState()
}

function confirmDeleteTask() {
  if (!props.task) return
  const accepted = window.confirm(`Delete "${props.task.title}"?`)
  if (!accepted) return
  emit('delete-task', props.task.id)
}

function openTaskLink(taskId: number | null | undefined) {
  if (!taskId) return
  emit('open-task', taskId)
}

function openConvertPanel() {
  if (!props.task || !canConvert.value) return
  convertError.value = null
  convertNotice.value = null
  convertTargetTypeId.value = props.task.type_id ?? props.taskTypes[0]?.id ?? null
  convertOpen.value = true
}

function closeConvertPanel() {
  convertOpen.value = false
  convertError.value = null
  convertLoading.value = false
}

function submitConvert() {
  if (!props.task || !convertTargetTypeId.value || convertLoading.value) return
  convertLoading.value = true
  convertError.value = null
  convertNotice.value = null
  emit('convert-task', props.task.id, convertTargetTypeId.value, (errorMessage?: string) => {
    convertLoading.value = false
    if (errorMessage) {
      convertError.value = errorMessage
      return
    }
    convertOpen.value = false
    convertNotice.value = 'Converted.'
  })
}

function deleteSubtask(subtask: Subtask) {
  if (!props.task) return
  markSaving()
  emit('delete-subtask', props.task.id, subtask.id)
  setSavedState()
}

function tokenAlias(name: string): string {
  return name.replace(/\s+/g, '')
}

function refreshMentionState() {
  const target = commentInputRef.value
  if (!target) {
    mentionState.value = null
    return
  }

  const caret = target.selectionStart ?? commentDraft.value.length
  const head = commentDraft.value.slice(0, caret)
  const match = /(^|\s)@([a-zA-Z0-9_]*)$/.exec(head)
  if (!match) {
    mentionState.value = null
    return
  }

  mentionState.value = {
    start: caret - match[2].length - 1,
    end: caret,
    query: match[2]
  }
}

function applyMention(user: User) {
  if (!mentionState.value) return
  const token = `@${tokenAlias(user.name)}`
  const before = commentDraft.value.slice(0, mentionState.value.start)
  const after = commentDraft.value.slice(mentionState.value.end)
  commentDraft.value = `${before}${token} ${after}`
  mentionState.value = null

  setTimeout(() => {
    commentInputRef.value?.focus()
    const pos = (before + `${token} `).length
    commentInputRef.value?.setSelectionRange(pos, pos)
  }, 0)
}

function extractMentions(text: string): string[] {
  const found = text.match(/@[a-zA-Z0-9_]+/g) ?? []
  return Array.from(new Set(found))
}

function commentAuthorName(comment: TaskComment): string {
  if (comment.author_id && props.currentUserId && comment.author_id === props.currentUserId) {
    return 'You'
  }
  if (comment.author?.name) return comment.author.name
  if (comment.author_id) {
    const matched = props.users.find((user) => user.id === comment.author_id)
    if (matched) return matched.name
  }
  return 'You'
}

function commentAuthorAvatar(comment: TaskComment): string | null {
  if (comment.author?.avatar_url) return comment.author.avatar_url

  if (comment.author_id) {
    const matched = props.users.find((user) => user.id === comment.author_id)
    if (matched?.avatar_url) return matched.avatar_url
  }

  if (comment.author_id && props.currentUserId && comment.author_id === props.currentUserId) {
    return currentUser.value?.avatar_url ?? null
  }

  return null
}

function avatarInitial(name: string): string {
  const trimmed = name.trim()
  if (!trimmed) return '?'
  return trimmed.charAt(0).toUpperCase()
}

function statusLabel(status: TaskStatus): string {
  return status.charAt(0).toUpperCase() + status.slice(1)
}

function toggleStatusMenu() {
  closeAssigneeMenu()
  statusMenuOpen.value = !statusMenuOpen.value
}

function closeStatusMenu() {
  statusMenuOpen.value = false
}

function selectStatus(status: TaskStatus) {
  if (isStatusOptionDisabled(status)) return
  form.status = status
  closeStatusMenu()
}

function toggleAssigneeMenu() {
  if (!props.isAdmin) return
  closeStatusMenu()
  assigneeMenuOpen.value = !assigneeMenuOpen.value
}

function closeAssigneeMenu() {
  assigneeMenuOpen.value = false
}

function selectAssignee(value: string | null) {
  form.assigned_to = value
  closeAssigneeMenu()
}

function handlePanelMenusOutsideClick(event: MouseEvent) {
  const target = event.target as Node | null
  if (!target) return

  if (statusPickerRef.value && !statusPickerRef.value.contains(target)) {
    closeStatusMenu()
  }
  if (assigneePickerRef.value && !assigneePickerRef.value.contains(target)) {
    closeAssigneeMenu()
  }
}

async function sendComment() {
  if (!props.currentUserId) {
    commentError.value = 'Select an active member before commenting.'
    return
  }

  const content = commentDraft.value.trim()
  if (!content || !props.task || isSendingComment.value) return

  const author = currentUser.value
  const signature = `${props.currentUserId}::${content}`
  const now = Date.now()
  if (signature === lastCommentSignature.value && now - lastCommentSentAt.value < 1200) {
    return
  }

  isSendingComment.value = true
  commentError.value = null
  try {
    const created = await api.createTaskComment(
      props.task.id,
      {
        author_id: author?.id ?? null,
        content,
        mentions: extractMentions(content),
      },
      props.currentUserId,
    )

    comments.value = dedupeComments([created, ...comments.value])
    commentDraft.value = ''
    mentionState.value = null
    lastCommentSignature.value = signature
    lastCommentSentAt.value = now

    setTimeout(() => {
      commentInputRef.value?.focus()
    }, 0)
  } catch (error) {
    commentError.value = error instanceof Error ? error.message : 'Failed to send comment.'
  } finally {
    isSendingComment.value = false
  }
}

function onCommentKeydown(event: KeyboardEvent) {
  if (event.isComposing || isCommentComposing.value) return

  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    void sendComment()
    return
  }

  if (event.key === 'Tab' && mentionSuggestions.value.length > 0 && mentionState.value) {
    event.preventDefault()
    applyMention(mentionSuggestions.value[0])
    return
  }

  if (event.key === 'Escape') {
    mentionState.value = null
  }
}

function onCommentPaste(event: ClipboardEvent) {
  const files = Array.from(event.clipboardData?.files ?? [])
  if (files.length === 0) return
  event.preventDefault()
  void addAttachments(files)
}

function formatCommentTime(iso: string): string {
  return new Intl.DateTimeFormat('en-GB', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit'
  }).format(new Date(iso))
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;')
}

function renderCommentHtml(text: string): string {
  const escaped = escapeHtml(text)
  return escaped.replace(/(^|\s)(@[a-zA-Z0-9_]+)/g, '$1<span class="comment-mention">$2</span>')
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

async function addAttachments(files: File[]) {
  if (!props.currentUserId) {
    attachmentError.value = 'Select an active member before uploading attachments.'
    return
  }
  if (!props.task || files.length === 0 || isUploadingAttachment.value) return

  attachmentError.value = null
  isUploadingAttachment.value = true
  try {
    for (const file of files) {
      if (file.size > MAX_ATTACHMENT_BYTES) {
        attachmentError.value = `"${file.name}" is larger than ${MAX_ATTACHMENT_MB} MB.`
        continue
      }

      const created = await api.createTaskAttachment(
        props.task.id,
        {
          uploaded_by: currentUser.value?.id ?? null,
          file,
        },
        props.currentUserId,
      )
      attachments.value.unshift(created)
    }
  } catch (error) {
    attachmentError.value = error instanceof Error ? error.message : 'Failed to upload attachment.'
  } finally {
    isUploadingAttachment.value = false
  }
}

function openAttachmentPicker() {
  attachmentInputRef.value?.click()
}

function openLinkComposer() {
  showLinkComposer.value = true
  setTimeout(() => {
    linkUrlInputRef.value?.focus()
  }, 0)
}

function closeLinkComposer() {
  showLinkComposer.value = false
  linkUrlDraft.value = ''
  linkNameDraft.value = ''
}

async function addLinkAttachment() {
  if (!props.currentUserId) {
    attachmentError.value = 'Select an active member before adding a link.'
    return
  }
  if (!props.task || isAddingLink.value || isUploadingAttachment.value) return

  const url = linkUrlDraft.value.trim()
  const name = linkNameDraft.value.trim()
  if (!url) return
  if (!/^https?:\/\//i.test(url)) {
    attachmentError.value = 'Link must start with http:// or https://.'
    return
  }

  attachmentError.value = null
  isAddingLink.value = true
  try {
    const created = await api.createTaskAttachmentLink(
      props.task.id,
      {
        url,
        name: name || null,
      },
      props.currentUserId,
    )
    attachments.value.unshift(created)
    closeLinkComposer()
  } catch (error) {
    attachmentError.value = error instanceof Error ? error.message : 'Failed to add link attachment.'
  } finally {
    isAddingLink.value = false
  }
}

function onAttachmentInput(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  void addAttachments(files)
  input.value = ''
}

async function removeAttachment(attachmentId: number) {
  if (!props.currentUserId || !props.task) return
  try {
    await api.deleteTaskAttachment(props.task.id, attachmentId, props.currentUserId)
    attachments.value = attachments.value.filter((item) => item.id !== attachmentId)
  } catch (error) {
    attachmentError.value = error instanceof Error ? error.message : 'Failed to delete attachment.'
  }
}

function isStatusOptionDisabled(status: TaskStatus): boolean {
  if (props.isAdmin) return false
  if (status === 'ready') return true
  if (status === 'done') return form.status !== 'ready' && form.status !== 'done'
  return false
}

function onAttachmentDragOver(event: DragEvent) {
  event.preventDefault()
  isAttachmentDragOver.value = true
}

function onAttachmentDragLeave(event: DragEvent) {
  event.preventDefault()
  isAttachmentDragOver.value = false
}

function onAttachmentDrop(event: DragEvent) {
  event.preventDefault()
  isAttachmentDragOver.value = false
  const files = Array.from(event.dataTransfer?.files ?? [])
  void addAttachments(files)
}

function onAttachmentPaste(event: ClipboardEvent) {
  const files = Array.from(event.clipboardData?.files ?? [])
  if (files.length === 0) return
  event.preventDefault()
  void addAttachments(files)
}

function isLinkAttachment(attachment: TaskAttachment): boolean {
  return attachment.mime_type === 'text/uri-list'
}

function attachmentMeta(attachment: TaskAttachment): string {
  if (!isLinkAttachment(attachment)) {
    return formatFileSize(attachment.size_bytes)
  }
  try {
    return new URL(attachment.data_url).host
  } catch {
    return attachment.data_url
  }
}

function syncSubtaskDraftsFromTask(task: Task) {
  const nextIds = new Set(task.subtasks.map((subtask) => subtask.id))
  Object.keys(subtaskDrafts).forEach((key) => {
    const id = Number(key)
    if (!nextIds.has(id)) delete subtaskDrafts[id]
  })
  task.subtasks.forEach((subtask) => {
    if (!(subtask.id in subtaskDrafts)) {
      subtaskDrafts[subtask.id] = subtask.content
    }
  })
}

function hydrateForm(task: Task) {
  isHydrating.value = true
  form.title = task.title
  form.description = task.description ?? ''
  form.status = task.status
  form.assigned_to = task.assigned_to
  form.shop_id = task.shop_id
  form.type_id = task.type_id
  form.due_date = task.due_date ?? ''
  form.priority = normalizePriorityForUi(task.priority)
  form.notes = task.notes ?? ''
  syncSubtaskDraftsFromTask(task)
  lastSavedSerialized.value = JSON.stringify(payloadFromTask(task))
  saveState.value = 'idle'
  setTimeout(() => {
    isHydrating.value = false
  }, 0)
}

watch(
  () => props.task?.id ?? null,
  (taskId, previousTaskId) => {
    closeStatusMenu()
    closeAssigneeMenu()
    clearSaveTimer()
    clearSubtaskTimers()
    convertOpen.value = false
    convertError.value = null
    convertLoading.value = false
    if (taskId !== previousTaskId) {
      convertNotice.value = null
    }

    const task = props.task
    if (!task) {
      sideLoadSeq += 1
      saveState.value = 'idle'
      comments.value = []
      attachments.value = []
      commentDraft.value = ''
      commentError.value = null
      attachmentError.value = null
      loadingSecondary.value = false
      return
    }

    hydrateForm(task)
    if (taskId !== previousTaskId) {
      comments.value = []
      attachments.value = []
      void loadTaskSideData(task.id)
    }
  },
  { immediate: true }
)

watch(
  () => props.task?.subtasks.map((subtask) => `${subtask.id}:${subtask.content}`).join('|') ?? '',
  () => {
    if (!props.task) return
    syncSubtaskDraftsFromTask(props.task)
  }
)

watch(
  form,
  () => {
    scheduleTaskAutoSave()
  },
  { deep: true }
)

watch(
  () => props.currentUserId,
  () => {
    if (!props.task) return
    void loadTaskSideData(props.task.id)
  }
)

watch(
  () => props.isAdmin,
  (isAdmin) => {
    if (!isAdmin) closeAssigneeMenu()
  }
)

onMounted(() => {
  if (typeof window === 'undefined') return
  window.addEventListener('mousedown', handlePanelMenusOutsideClick)
})

onBeforeUnmount(() => {
  if (typeof window !== 'undefined') {
    window.removeEventListener('mousedown', handlePanelMenusOutsideClick)
  }
  clearSaveTimer()
  clearSaveStateTimer()
  clearSubtaskTimers()
})
</script>

<template>
  <aside class="detail-panel">
    <div v-if="!props.task" class="detail-empty">
      <h3>Select a task</h3>
      <p>Open any task in the middle panel to view and edit details.</p>
    </div>

    <template v-else>
      <header class="detail-header">
        <div>
          <h2>{{ props.task.title }}</h2>
          <p class="detail-subtitle">Inline edits for fast execution.</p>
        </div>
        <div class="detail-header-right">
          <button v-if="canConvert" class="ghost-btn" @click="openConvertPanel">Convert</button>
          <span v-if="saveState !== 'idle'" class="save-indicator" :class="saveState">
            {{ saveState === 'saving' ? 'Saving…' : 'Saved' }}
          </span>
          <button class="ghost-btn danger" @click="confirmDeleteTask">Delete</button>
        </div>
      </header>

      <p v-if="convertNotice" class="convert-notice">{{ convertNotice }}</p>

      <section v-if="props.task.parent_task_id || props.task.latest_converted_task_id" class="task-lineage">
        <button
          v-if="props.task.parent_task_id"
          class="lineage-link"
          type="button"
          @click="openTaskLink(props.task.parent_task_id)"
        >
          Converted from #{{ props.task.parent_task_id }}
        </button>
        <button
          v-if="props.task.latest_converted_task_id"
          class="lineage-link"
          type="button"
          @click="openTaskLink(props.task.latest_converted_task_id)"
        >
          Converted to #{{ props.task.latest_converted_task_id }}
        </button>
      </section>

      <section v-if="convertOpen" class="convert-panel">
        <label>
          Convert to task type
          <select v-model.number="convertTargetTypeId">
            <option :value="null">Select task type</option>
            <option v-for="taskType in props.taskTypes" :key="taskType.id" :value="taskType.id">
              {{ taskType.name }}
            </option>
          </select>
        </label>
        <div class="convert-actions">
          <button class="primary-btn" type="button" :disabled="!convertTargetTypeId || convertLoading" @click="submitConvert">
            {{ convertLoading ? 'Converting…' : 'Confirm Convert' }}
          </button>
          <button class="ghost-btn" type="button" :disabled="convertLoading" @click="closeConvertPanel">Cancel</button>
        </div>
        <p v-if="convertError" class="attachment-error">{{ convertError }}</p>
      </section>

      <div class="detail-form">
        <label>
          Title
          <input v-model="form.title" type="text" />
        </label>

        <label>
          Description
          <textarea v-model="form.description" rows="3" />
        </label>

        <div class="detail-grid">
          <label class="detail-field detail-field-with-foot">
            <span>Status</span>
            <div
              ref="statusPickerRef"
              class="status-picker detail-status-picker"
              @keydown.escape.prevent="closeStatusMenu"
            >
              <button
                class="status-trigger"
                type="button"
                aria-haspopup="listbox"
                :aria-expanded="statusMenuOpen ? 'true' : 'false'"
                aria-label="Status"
                @click="toggleStatusMenu"
              >
                <span class="status-chip status-picker-chip" :class="`status-${form.status}`">
                  {{ statusLabel(form.status) }}
                </span>
                <span class="assignee-caret">⌄</span>
              </button>

              <ul v-if="statusMenuOpen" class="status-menu" role="listbox" aria-label="Status options">
                <li v-for="status in statusOptions" :key="status">
                  <button
                    class="status-option"
                    :class="{
                      selected: status === form.status,
                      disabled: isStatusOptionDisabled(status)
                    }"
                    type="button"
                    role="option"
                    :aria-selected="status === form.status ? 'true' : 'false'"
                    :disabled="isStatusOptionDisabled(status)"
                    @click="selectStatus(status)"
                  >
                    <span class="status-chip status-menu-chip" :class="`status-${status}`">
                      {{ statusLabel(status) }}
                    </span>
                    <span v-if="status === form.status" class="status-option-check">✓</span>
                  </button>
                </li>
              </ul>
            </div>
            <small v-if="approvalState" class="detail-field-foot approval-hint" :class="`approval-${approvalState.tone}`">
              {{ approvalState.label }}
            </small>
            <small v-else class="detail-field-foot detail-field-foot-empty" aria-hidden="true">&nbsp;</small>
          </label>

          <label class="detail-field detail-field-with-foot">
            <span>Assignee</span>
            <div
              ref="assigneePickerRef"
              class="assignee-picker detail-assignee-picker"
              @keydown.escape.prevent="closeAssigneeMenu"
            >
              <button
                class="assignee-trigger"
                type="button"
                :disabled="!props.isAdmin"
                aria-haspopup="listbox"
                :aria-expanded="assigneeMenuOpen ? 'true' : 'false'"
                aria-label="Assignee"
                @click="toggleAssigneeMenu"
              >
                <span class="assignee-avatar">
                  <img
                    v-if="selectedAssigneeOption.avatarUrl"
                    :src="selectedAssigneeOption.avatarUrl"
                    :alt="selectedAssigneeOption.name"
                  />
                  <span v-else>{{ avatarInitial(selectedAssigneeOption.name) }}</span>
                </span>
                <span class="assignee-name">{{ selectedAssigneeOption.name }}</span>
                <span class="assignee-caret">⌄</span>
              </button>

              <ul
                v-if="assigneeMenuOpen && props.isAdmin"
                class="assignee-menu"
                role="listbox"
                aria-label="Assignee options"
              >
                <li v-for="option in assigneeOptions" :key="option.id ?? 'none'">
                  <button
                    class="assignee-option"
                    :class="{ selected: option.id === selectedAssigneeOption.id }"
                    type="button"
                    role="option"
                    :aria-selected="option.id === selectedAssigneeOption.id ? 'true' : 'false'"
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
            <small class="detail-field-foot detail-field-foot-empty" aria-hidden="true">&nbsp;</small>
          </label>

          <label>
            Shop
            <select v-model.number="form.shop_id" @change="onShopChanged">
              <option :value="null">None</option>
              <option v-for="shop in props.shops" :key="shop.id" :value="shop.id">{{ shop.name }}</option>
            </select>
          </label>

          <label>
            Type
            <select v-model.number="form.type_id" @change="onTypeChanged">
              <option :value="null">None</option>
              <option v-for="taskType in props.taskTypes" :key="taskType.id" :value="taskType.id">{{ taskType.name }}</option>
            </select>
          </label>

          <label>
            Due
            <input v-model="form.due_date" type="date" />
          </label>

          <label>
            Priority
            <select v-model="form.priority" class="priority-select" :class="`priority-${form.priority}`">
              <option value="low">low</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
            </select>
          </label>

        </div>

        <label v-if="SHOW_NOTES">
          Notes
          <textarea v-model="form.notes" rows="3" />
        </label>
      </div>

      <section v-if="SHOW_CHECKLIST" class="checklist">
        <h3>Checklist</h3>

        <ul>
          <li v-for="subtask in props.task.subtasks" :key="subtask.id" class="checklist-item" :class="{ done: subtask.is_done }">
            <label class="checklist-main">
              <input type="checkbox" :checked="subtask.is_done" @change="toggleSubtask(subtask)" />
              <input
                v-model="subtaskDrafts[subtask.id]"
                class="subtask-input"
                type="text"
                :class="{ done: subtask.is_done }"
                @input="scheduleSubtaskContentSave(subtask)"
                @blur="onSubtaskBlur(subtask)"
              />
            </label>
            <button class="subtask-delete" title="Delete checklist item" @click="deleteSubtask(subtask)">×</button>
          </li>
        </ul>

        <div class="subtask-create">
          <input v-model="newSubtask" type="text" placeholder="Add checklist item" @keyup.enter="addSubtask" />
          <button class="primary-btn" @click="addSubtask">Add</button>
        </div>
      </section>

      <section v-if="SHOW_COMMENTS" class="detail-secondary comments-section">
        <h3>Comments</h3>

        <div class="comment-compose">
          <div class="comment-input-wrap">
            <textarea
              ref="commentInputRef"
              v-model="commentDraft"
              rows="3"
              placeholder="Write a comment. Enter to send, Shift+Enter for new line, @mention teammates"
              :disabled="isSendingComment || loadingSecondary"
              @input="refreshMentionState"
              @click="refreshMentionState"
              @keyup="refreshMentionState"
              @keydown="onCommentKeydown"
              @compositionstart="isCommentComposing = true"
              @compositionend="isCommentComposing = false"
              @paste="onCommentPaste"
            />

            <div v-if="mentionState && mentionSuggestions.length > 0" class="mention-menu">
              <button
                v-for="user in mentionSuggestions"
                :key="user.id"
                class="mention-item"
                :disabled="isSendingComment || loadingSecondary"
                @mousedown.prevent="applyMention(user)"
              >
                <span class="mention-item-content">
                  <span class="mention-avatar">
                    <img v-if="user.avatar_url" :src="user.avatar_url" :alt="user.name" />
                    <span v-else>{{ avatarInitial(user.name) }}</span>
                  </span>
                  <span>@{{ tokenAlias(user.name) }}</span>
                </span>
              </button>
            </div>
          </div>
        </div>

        <p v-if="commentError" class="attachment-error">{{ commentError }}</p>
        <p v-if="loadingSecondary" class="secondary-empty">Loading comments…</p>

        <ul v-else-if="comments.length > 0" class="comment-list">
          <li v-for="comment in comments" :key="comment.id" class="comment-item">
            <div class="comment-head">
              <div class="comment-head-left">
                <span class="comment-avatar">
                  <img v-if="commentAuthorAvatar(comment)" :src="commentAuthorAvatar(comment) ?? ''" :alt="commentAuthorName(comment)" />
                  <span v-else>{{ avatarInitial(commentAuthorName(comment)) }}</span>
                </span>
                <strong>{{ commentAuthorName(comment) }}</strong>
              </div>
              <span>{{ formatCommentTime(comment.created_at) }}</span>
            </div>
            <p class="comment-body" v-html="renderCommentHtml(comment.content)" />
          </li>
        </ul>

        <p v-else class="secondary-empty">No comments yet.</p>
      </section>

      <section class="detail-secondary attachments-section">
        <h3>Attachments</h3>

        <input ref="attachmentInputRef" type="file" multiple hidden :disabled="isUploadingAttachment || loadingSecondary" @change="onAttachmentInput" />

        <div
          class="attachment-dropzone"
          :class="{ dragover: isAttachmentDragOver }"
          @dragover="onAttachmentDragOver"
          @dragleave="onAttachmentDragLeave"
          @drop="onAttachmentDrop"
          @paste="onAttachmentPaste"
        >
          <p>
            Drop files here, paste image/file, or
            <button class="link-btn" :disabled="isUploadingAttachment || loadingSecondary" @click="openAttachmentPicker">browse</button>
            (max {{ MAX_ATTACHMENT_MB }} MB)
          </p>
        </div>

        <div class="attachment-actions">
          <button
            v-if="!showLinkComposer"
            class="link-btn"
            :disabled="isAddingLink || isUploadingAttachment || loadingSecondary"
            @click="openLinkComposer"
          >
            Add link
          </button>

          <div v-else class="attachment-link-form">
            <input
              ref="linkUrlInputRef"
              v-model="linkUrlDraft"
              class="attachment-link-url"
              type="url"
              placeholder="https://example.com"
              :disabled="isAddingLink || isUploadingAttachment || loadingSecondary"
              @keyup.enter.prevent="addLinkAttachment"
            />
            <input
              v-model="linkNameDraft"
              type="text"
              placeholder="Label (optional)"
              :disabled="isAddingLink || isUploadingAttachment || loadingSecondary"
              @keyup.enter.prevent="addLinkAttachment"
            />
            <button
              class="primary-btn"
              type="button"
              :disabled="!linkUrlDraft.trim() || isAddingLink || isUploadingAttachment || loadingSecondary"
              @click="addLinkAttachment"
            >
              {{ isAddingLink ? 'Adding…' : 'Save link' }}
            </button>
            <button class="ghost-btn" type="button" :disabled="isAddingLink" @click="closeLinkComposer">Cancel</button>
          </div>
        </div>

        <p v-if="attachmentError" class="attachment-error">{{ attachmentError }}</p>
        <p v-if="isUploadingAttachment || isAddingLink" class="secondary-empty">
          {{ isAddingLink ? 'Adding link…' : 'Uploading…' }}
        </p>
        <p v-if="loadingSecondary" class="secondary-empty">Loading attachments…</p>

        <ul v-if="!loadingSecondary && attachments.length > 0" class="attachment-grid">
          <li v-for="attachment in attachments" :key="attachment.id" class="attachment-item">
            <a
              class="attachment-card"
              :class="{ link: isLinkAttachment(attachment) }"
              :href="attachment.data_url"
              :download="isLinkAttachment(attachment) ? undefined : attachment.name"
              target="_blank"
              rel="noreferrer noopener"
            >
              <div class="attachment-thumb">
                <img v-if="attachment.is_image" :src="attachment.data_url" :alt="attachment.name" />
                <span v-else>{{ isLinkAttachment(attachment) ? 'LINK' : 'FILE' }}</span>
              </div>
              <div class="attachment-info">
                <strong>{{ attachment.name }}</strong>
                <small>{{ attachmentMeta(attachment) }}</small>
              </div>
            </a>
            <button class="attachment-remove" @click="removeAttachment(attachment.id)">×</button>
          </li>
        </ul>

        <p v-else-if="!loadingSecondary" class="secondary-empty">No attachments yet.</p>
      </section>
    </template>
  </aside>
</template>
