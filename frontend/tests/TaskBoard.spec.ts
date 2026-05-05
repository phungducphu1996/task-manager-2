import { render, waitFor } from '@testing-library/vue'
import { createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import { beforeAll, vi } from 'vitest'
import TaskBoard from '../src/components/TaskBoard.vue'
import { useTaskStore } from '../src/stores/taskStore'

const mockApi = vi.hoisted(() => ({
  login: vi.fn(),
  getMe: vi.fn().mockResolvedValue({ id: 'admin', username: 'admin', name: 'Admin', role: 'admin' }),
  getUsers: vi.fn().mockResolvedValue([]),
  getShops: vi.fn().mockResolvedValue([]),
  getTaskTypes: vi.fn().mockResolvedValue([]),
  getTasks: vi.fn().mockResolvedValue({
    groups: [],
    counts: { inbox: 0, today: 0, upcoming: 0, anytime: 0, review: 0, logbook: 0 }
  }),
  getTask: vi.fn().mockResolvedValue({
    id: 63,
    title: 'Bluey Collection',
    description: null,
    status: 'todo',
    assigned_to: null,
    created_by: null,
    shop_id: null,
    type_id: null,
    scheduled_date: null,
    due_date: null,
    priority: 'medium',
    notes: null,
    is_someday: false,
    list_order: 1,
    created_at: '2026-04-28T00:00:00Z',
    updated_at: '2026-04-28T00:00:00Z',
    subtasks: []
  }),
  getTaskComments: vi.fn().mockResolvedValue([]),
  getTaskAttachments: vi.fn().mockResolvedValue([])
}))

vi.mock('../src/services/api', () => ({
  api: mockApi,
  getStoredToken: vi.fn(() => 'token'),
  setStoredToken: vi.fn(),
  flattenGroups: vi.fn((groups) => groups.flatMap((group: { tasks: unknown[] }) => group.tasks)),
  default: mockApi
}))

describe('TaskBoard route task query', () => {
  beforeAll(() => {
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn()
      }))
    })
  })

  it('opens task detail from ?task query', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/:view', component: TaskBoard }]
    })
    await router.push('/today?task=63')
    await router.isReady()

    render(TaskBoard, {
      global: {
        plugins: [
          router,
          createPinia()
        ]
      }
    })

    const store = useTaskStore()
    await waitFor(() => {
      expect(store.selectedTask?.id).toBe(63)
    })
  })
})
