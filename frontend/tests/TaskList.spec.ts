import { fireEvent, render } from '@testing-library/vue'
import TaskList from '../src/components/TaskList.vue'

const task = {
  id: 1,
  title: 'Run ads test',
  description: null,
  status: 'todo',
  assigned_to: 'user-1',
  created_by: 'user-1',
  shop_id: 2,
  type_id: 2,
  scheduled_date: '2026-03-20',
  due_date: null,
  priority: 'medium',
  notes: null,
  is_someday: false,
  list_order: 1,
  created_at: '2026-03-19T00:00:00Z',
  updated_at: '2026-03-19T00:00:00Z',
  shop: { id: 2, name: 'Yessey' },
  task_type: { id: 2, name: 'Ads' },
  subtasks: []
}

describe('TaskList', () => {
  it('renders groups and emits select', async () => {
    const { getByText, emitted } = render(TaskList, {
      props: {
        groups: [{ key: 'today', title: 'Today', tasks: [task] }],
        selectedTaskId: null,
        selectedTaskIds: [],
        loading: false,
      }
    })

    await fireEvent.click(getByText('Run ads test'))

    expect(getByText('Today')).toBeTruthy()
    expect(emitted().select).toBeTruthy()
  })
})
