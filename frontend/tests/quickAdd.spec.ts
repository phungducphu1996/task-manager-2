import { describe, expect, it } from 'vitest'
import { analyzeQuickTaskInput, applyQuickAddSuggestion, parseQuickTaskInput } from '../src/utils/quickAdd'

describe('parseQuickTaskInput', () => {
  const users = [
    { id: 'u-1', username: 'linh', name: 'Linh', role: 'designer' as const },
    { id: 'u-2', username: 'ngoc', name: 'Ngoc', role: 'seller' as const }
  ]
  const shops = [
    { id: 1, name: 'Gemi' },
    { id: 2, name: 'AmzMage' }
  ]

  it('parses structured tokens and keeps clean title', () => {
    const payload = parseQuickTaskInput('Design tanktop #Gemi @Ngoc !high today', {
      users,
      shops,
      activeAssigneeId: 'u-1',
      activeRole: 'admin',
      view: 'inbox'
    })

    expect(payload).not.toBeNull()
    expect(payload?.title).toBe('Design tanktop')
    expect(payload?.shop_id).toBe(1)
    expect(payload?.assigned_to).toBe('u-2')
    expect(payload?.priority).toBe('high')
    expect(payload?.due_date).toBeTruthy()
  })

  it('keeps unknown tokens in title and falls back to active assignee', () => {
    const payload = parseQuickTaskInput('Fix listing #Unknown @Nope', {
      users,
      shops,
      activeAssigneeId: 'u-1',
      activeRole: 'designer',
      view: 'anytime'
    })

    expect(payload).not.toBeNull()
    expect(payload?.title).toBe('Fix listing #Unknown @Nope')
    expect(payload?.assigned_to).toBe('u-1')
    expect(payload?.shop_id).toBeUndefined()
  })

  it('parses dd/mm date token into ISO', () => {
    const payload = parseQuickTaskInput('Fix listing 24/03/2026', {
      users,
      shops,
      activeAssigneeId: 'u-1',
      activeRole: 'designer',
      view: 'inbox'
    })

    expect(payload?.title).toBe('Fix listing')
    expect(payload?.due_date).toBe('2026-03-24')
    expect(payload?.scheduled_date).toBeNull()
  })

  it('returns suggestions and preview chips while typing', () => {
    const analysis = analyzeQuickTaskInput('Design #Ge', {
      users,
      shops,
      activeAssigneeId: 'u-1',
      activeRole: 'designer',
      view: 'today'
    })

    expect(analysis.suggestions.some((item) => item.kind === 'shop')).toBe(true)
    expect(analysis.chips.some((chip) => chip.text.includes('(default)'))).toBe(true)
  })

  it('applies selected suggestion to current token', () => {
    const output = applyQuickAddSuggestion('Design #Ge', '#Gemi')
    expect(output).toBe('Design #Gemi ')
  })
})
