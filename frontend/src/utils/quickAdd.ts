import type { Shop, TaskPayload, TaskPriority, TaskView, User } from '../types'

export interface ParseQuickTaskOptions {
  users: User[]
  shops: Shop[]
  activeAssigneeId: string | null
  activeRole: User['role'] | null
  view: TaskView
}

export interface QuickAddPreviewChip {
  key: string
  text: string
  isDefault?: boolean
}

export interface QuickAddSuggestion {
  key: string
  token: string
  label: string
  kind: 'shop' | 'assignee' | 'priority' | 'date'
}

export interface QuickAddAnalysis {
  payload: TaskPayload | null
  chips: QuickAddPreviewChip[]
  suggestions: QuickAddSuggestion[]
}

const PRIORITY_VALUES: TaskPriority[] = ['low', 'medium', 'high', 'urgent']

interface TokenMatchState {
  shop: Shop | null
  assignee: User | null
  priority: TaskPriority | null
  parsedDate: string | null
  title: string
}

interface ActiveTokenRange {
  token: string
  start: number
  end: number
}

function toLocalIsoDate(value: Date): string {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`
}

function addDays(base: Date, days: number): Date {
  const next = new Date(base)
  next.setDate(next.getDate() + days)
  return next
}

function lookupKey(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, '')
    .trim()
}

function tokenAlias(value: string): string {
  return value.replace(/\s+/g, '')
}

function parseDateToken(rawToken: string): string | null {
  const lower = rawToken.toLowerCase()
  const today = new Date()

  if (lower === 'today') return toLocalIsoDate(today)
  if (lower === 'tomorrow') return toLocalIsoDate(addDays(today, 1))

  if (/^\d{4}-\d{2}-\d{2}$/.test(rawToken)) {
    const [year, month, day] = rawToken.split('-').map(Number)
    const parsed = new Date(year, month - 1, day)
    if (toLocalIsoDate(parsed) === rawToken) return rawToken
    return null
  }

  const slash = rawToken.match(/^(\d{1,2})\/(\d{1,2})(?:\/(\d{2,4}))?$/)
  if (!slash) return null

  const day = Number(slash[1])
  const month = Number(slash[2])
  let year = slash[3] ? Number(slash[3]) : today.getFullYear()
  if (year < 100) year += 2000

  const parsed = new Date(year, month - 1, day)
  if (parsed.getFullYear() !== year || parsed.getMonth() !== month - 1 || parsed.getDate() !== day) {
    return null
  }
  return toLocalIsoDate(parsed)
}

function parseTokenMatches(rawInput: string, options: ParseQuickTaskOptions): TokenMatchState {
  const input = rawInput.trim()
  if (!input) {
    return {
      shop: null,
      assignee: null,
      priority: null,
      parsedDate: null,
      title: ''
    }
  }

  let shop: Shop | null = null
  let assignee: User | null = null
  let priority: TaskPriority | null = null
  let parsedDate: string | null = null
  const titleParts: string[] = []

  for (const token of input.split(/\s+/)) {
    if (!token) continue

    if (token.startsWith('#')) {
      const candidate = lookupKey(token.slice(1))
      const matchedShop = options.shops.find((item) => lookupKey(item.name) === candidate)
      if (matchedShop) {
        shop = matchedShop
        continue
      }
    }

    if (token.startsWith('@')) {
      const candidate = lookupKey(token.slice(1))
      const matchedUser = options.users.find((item) => lookupKey(item.name) === candidate)
      if (matchedUser) {
        if (options.activeRole === 'admin' || matchedUser.id === options.activeAssigneeId) {
          assignee = matchedUser
        }
        continue
      }
    }

    if (token.startsWith('!')) {
      const candidate = lookupKey(token.slice(1)) as TaskPriority
      if (PRIORITY_VALUES.includes(candidate)) {
        priority = candidate
        continue
      }
    }

    const maybeDate = parseDateToken(token)
    if (maybeDate) {
      parsedDate = maybeDate
      continue
    }

    titleParts.push(token)
  }

  return {
    shop,
    assignee,
    priority,
    parsedDate,
    title: titleParts.join(' ').trim()
  }
}

function activeTokenRange(rawInput: string): ActiveTokenRange | null {
  if (!rawInput || /\s$/.test(rawInput)) return null
  const start = rawInput.search(/\S+$/)
  if (start === -1) return null
  return {
    token: rawInput.slice(start),
    start,
    end: rawInput.length
  }
}

function buildSuggestions(rawInput: string, options: ParseQuickTaskOptions): QuickAddSuggestion[] {
  const active = activeTokenRange(rawInput)
  if (!active) return []

  const token = active.token
  const lower = token.toLowerCase()
  const list: QuickAddSuggestion[] = []

  if (token.startsWith('#')) {
    const query = lookupKey(token.slice(1))
    options.shops
      .filter((shop) => lookupKey(shop.name).includes(query))
      .slice(0, 6)
      .forEach((shop) => {
        list.push({
          key: `shop-${shop.id}`,
          token: `#${tokenAlias(shop.name)}`,
          label: `# ${shop.name}`,
          kind: 'shop'
        })
      })
    return list
  }

  if (token.startsWith('@')) {
    const query = lookupKey(token.slice(1))
    options.users
      .filter((user) => options.activeRole === 'admin' || user.id === options.activeAssigneeId)
      .filter((user) => lookupKey(user.name).includes(query))
      .slice(0, 6)
      .forEach((user) => {
        list.push({
          key: `assignee-${user.id}`,
          token: `@${tokenAlias(user.name)}`,
          label: `@ ${user.name}`,
          kind: 'assignee'
        })
      })
    return list
  }

  if (token.startsWith('!')) {
    const query = lookupKey(token.slice(1))
    PRIORITY_VALUES.filter((priority) => priority.includes(query)).forEach((priority) => {
      list.push({
        key: `priority-${priority}`,
        token: `!${priority}`,
        label: `! ${priority}`,
        kind: 'priority'
      })
    })
    return list
  }

  const dateCandidate = parseDateToken(token)
  if (dateCandidate && dateCandidate !== token) {
    list.push({
      key: 'date-normalized',
      token: dateCandidate,
      label: `Date ${dateCandidate}`,
      kind: 'date'
    })
    return list
  }

  const dateKeywords = ['today', 'tomorrow']
  if (dateKeywords.some((item) => item.startsWith(lower)) || /^\d/.test(token)) {
    dateKeywords
      .filter((item) => item.startsWith(lower))
      .forEach((item) => {
        list.push({
          key: `date-${item}`,
          token: item,
          label: `Date ${item}`,
          kind: 'date'
        })
      })
  }

  return list.slice(0, 6)
}

function buildPayload(state: TokenMatchState, options: ParseQuickTaskOptions): TaskPayload | null {
  if (!state.title) return null

  const payload: TaskPayload = {
    title: state.title,
    assigned_to: state.assignee?.id ?? options.activeAssigneeId,
    created_by: options.activeAssigneeId ?? state.assignee?.id
  }

  if (options.activeRole !== 'admin') {
    payload.assigned_to = options.activeAssigneeId
  }

  if (state.shop) payload.shop_id = state.shop.id
  if (state.priority) payload.priority = state.priority

  if (state.parsedDate) {
    payload.due_date = state.parsedDate
  } else if (options.view === 'today') {
    payload.due_date = toLocalIsoDate(new Date())
  }

  payload.scheduled_date = null
  payload.is_someday = false

  return payload
}

function buildPreviewChips(state: TokenMatchState, options: ParseQuickTaskOptions): QuickAddPreviewChip[] {
  const chips: QuickAddPreviewChip[] = []
  const activeUser = options.users.find((item) => item.id === options.activeAssigneeId) ?? null

  if (state.shop) chips.push({ key: 'shop', text: `#${state.shop.name}` })

  if (state.assignee) {
    chips.push({ key: 'assignee', text: `@${state.assignee.name}` })
  } else if (activeUser) {
    chips.push({ key: 'assignee-default', text: `@${activeUser.name} (default)`, isDefault: true })
  }

  if (state.priority) chips.push({ key: 'priority', text: `!${state.priority}` })

  if (state.parsedDate) {
    chips.push({ key: 'date', text: `Due ${state.parsedDate}` })
  } else if (options.view === 'today') {
    chips.push({ key: 'date-default', text: 'Due Today (default)', isDefault: true })
  }

  return chips
}

export function analyzeQuickTaskInput(rawInput: string, options: ParseQuickTaskOptions): QuickAddAnalysis {
  const state = parseTokenMatches(rawInput, options)
  return {
    payload: buildPayload(state, options),
    chips: buildPreviewChips(state, options),
    suggestions: buildSuggestions(rawInput, options)
  }
}

export function parseQuickTaskInput(rawInput: string, options: ParseQuickTaskOptions): TaskPayload | null {
  return analyzeQuickTaskInput(rawInput, options).payload
}

export function applyQuickAddSuggestion(rawInput: string, suggestionToken: string): string {
  const active = activeTokenRange(rawInput)
  if (!active) {
    if (!rawInput.trim()) return `${suggestionToken} `
    return `${rawInput.trimEnd()} ${suggestionToken} `
  }

  const before = rawInput.slice(0, active.start)
  return `${before}${suggestionToken} `
}
