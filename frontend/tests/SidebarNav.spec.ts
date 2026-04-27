import { fireEvent, render } from '@testing-library/vue'
import SidebarNav from '../src/components/SidebarNav.vue'

describe('SidebarNav', () => {
  it('emits view change and filters', async () => {
    const { getByText, getByLabelText, getByTestId, emitted } = render(SidebarNav, {
      props: {
        currentView: 'today',
        users: [{ id: 'user-1', username: 'linh', name: 'Linh', role: 'designer' }],
        shops: [{ id: 10, name: 'AmzMage' }],
        taskTypes: [{ id: 20, name: 'Design' }],
        viewCounts: { inbox: 13, today: 3 },
        assigneeId: 'user-1',
        activeRole: 'designer',
        lockAssignee: false,
        shopFilter: null,
        typeFilter: null
      }
    })

    expect(getByTestId('nav-item-today').className).toContain('active')
    expect(getByTestId('nav-icon-today')).toBeTruthy()
    expect(getByTestId('nav-icon-inbox')).toBeTruthy()
    expect(getByTestId('nav-icon-anytime')).toBeTruthy()
    expect(getByTestId('nav-count-inbox').textContent).toBe('13')
    expect(getByTestId('nav-count-today').textContent).toBe('3')

    await fireEvent.click(getByText('Upcoming'))
    await fireEvent.update(getByLabelText('Shop'), '10')

    expect(emitted()['change-view'][0]).toEqual(['upcoming'])
    expect(emitted()['change-shop'][0]).toEqual([10])
  })

  it('shows manage button for admin and emits open event', async () => {
    const { getByRole, emitted } = render(SidebarNav, {
      props: {
        currentView: 'today',
        users: [{ id: 'user-1', username: 'admin', name: 'Admin', role: 'admin' }],
        shops: [],
        taskTypes: [{ id: 20, name: 'Design' }],
        viewCounts: { review: 2 },
        assigneeId: null,
        activeRole: 'admin',
        lockAssignee: false,
        shopFilter: null,
        typeFilter: null
      }
    })

    await fireEvent.click(getByRole('button', { name: 'Manage' }))

    expect(emitted()['open-manage'][0]).toEqual([])
  })

  it('supports avatar assignee picker and emits change event', async () => {
    const { getByText, emitted } = render(SidebarNav, {
      props: {
        currentView: 'inbox',
        users: [
          { id: 'user-1', username: 'linh', name: 'Linh', role: 'designer', avatar_url: null },
          { id: 'user-2', username: 'quang', name: 'Quang', role: 'seller', avatar_url: null }
        ],
        shops: [],
        taskTypes: [],
        viewCounts: { inbox: 2 },
        assigneeId: 'user-1',
        activeRole: 'admin',
        lockAssignee: false,
        shopFilter: null,
        typeFilter: null
      }
    })

    await fireEvent.click(getByText('Linh'))
    await fireEvent.click(getByText('All members'))

    expect(emitted()['change-assignee'][0]).toEqual([null])
  })
})
