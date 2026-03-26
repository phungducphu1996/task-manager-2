import { fireEvent, render } from '@testing-library/vue'
import TaskRow from '../src/components/TaskRow.vue'

const task = {
  id: 1,
  title: 'Design banner',
  description: null,
  status: 'todo',
  assigned_to: 'user-1',
  created_by: 'user-1',
  shop_id: 1,
  type_id: 1,
  scheduled_date: null,
  due_date: '2026-03-19',
  priority: 'high',
  notes: null,
  is_someday: false,
  list_order: 1,
  created_at: '2026-03-19T00:00:00Z',
  updated_at: '2026-03-19T00:00:00Z',
  shop: { id: 1, name: 'AmzMage' },
  task_type: { id: 1, name: 'Design' },
  subtasks: []
}

describe('TaskRow', () => {
  it('emits select and check', async () => {
    const { getByText, getByRole, emitted } = render(TaskRow, {
      props: { task, selected: false, checked: false }
    })

    await fireEvent.click(getByText('Design banner'))
    await fireEvent.click(getByRole('checkbox'))

    expect(emitted().select).toBeTruthy()
    expect(emitted().check).toBeTruthy()
  })
})
