from datetime import date, timedelta


def actor_headers(user_id: str) -> dict[str, str]:
    return {'X-Actor-Id': user_id}


def select_users(users: list[dict]) -> tuple[dict, dict, dict]:
    admin = next(user for user in users if user['role'] == 'admin')
    members = [user for user in users if user['role'] != 'admin']
    return admin, members[0], members[1]


def test_task_crud_and_status_flow(client) -> None:
    users = client.get('/users').json()
    admin, member, _ = select_users(users)
    shops = client.get('/shops').json()
    types = client.get('/task-types').json()

    payload = {
        'title': 'Design banner',
        'description': 'Create hero banner',
        'assigned_to': member['id'],
        'created_by': admin['id'],
        'shop_id': shops[0]['id'],
        'type_id': types[0]['id'],
        'scheduled_date': date.today().isoformat(),
        'due_date': date.today().isoformat(),
        'priority': 'high',
        'notes': 'Need before noon',
    }

    create_res = client.post('/tasks', json=payload, headers=actor_headers(admin['id']))
    assert create_res.status_code == 201
    task_id = create_res.json()['id']

    today_res = client.get('/tasks', params={'view': 'today'}, headers=actor_headers(member['id']))
    assert today_res.status_code == 200
    all_ids = [task['id'] for group in today_res.json()['groups'] for task in group['tasks']]
    assert task_id in all_ids

    move_to_review = client.patch(
        f'/tasks/{task_id}/status',
        json={'status': 'review'},
        headers=actor_headers(member['id']),
    )
    assert move_to_review.status_code == 200
    assert move_to_review.json()['status'] == 'review'

    approve = client.patch(
        f'/tasks/{task_id}/status',
        json={'status': 'ready'},
        headers=actor_headers(admin['id']),
    )
    assert approve.status_code == 200
    assert approve.json()['status'] == 'ready'

    mark_done = client.patch(
        f'/tasks/{task_id}/status',
        json={'status': 'done'},
        headers=actor_headers(admin['id']),
    )
    assert mark_done.status_code == 200
    assert mark_done.json()['status'] == 'done'

    today_after_done = client.get('/tasks', params={'view': 'today'}, headers=actor_headers(member['id']))
    all_ids_after_done = [task['id'] for group in today_after_done.json()['groups'] for task in group['tasks']]
    assert task_id not in all_ids_after_done

    logbook_res = client.get('/tasks', params={'view': 'logbook'}, headers=actor_headers(member['id']))
    assert logbook_res.status_code == 200
    logbook_ids = [task['id'] for group in logbook_res.json()['groups'] for task in group['tasks']]
    assert task_id in logbook_ids


def test_filter_and_reorder(client) -> None:
    users = client.get('/users').json()
    admin, member, _ = select_users(users)
    shops = client.get('/shops').json()
    types = client.get('/task-types').json()

    base = {
        'assigned_to': member['id'],
        'created_by': admin['id'],
        'scheduled_date': (date.today() + timedelta(days=1)).isoformat(),
        'priority': 'medium',
    }

    t1 = client.post(
        '/tasks',
        json={**base, 'title': 'Ads A', 'shop_id': shops[0]['id'], 'type_id': types[1]['id']},
        headers=actor_headers(admin['id']),
    ).json()
    t2 = client.post(
        '/tasks',
        json={**base, 'title': 'Ads B', 'shop_id': shops[1]['id'], 'type_id': types[1]['id']},
        headers=actor_headers(admin['id']),
    ).json()

    filtered = client.get(
        '/tasks',
        params={
            'view': 'upcoming',
            'shop_id': shops[0]['id'],
            'type_id': types[1]['id'],
        },
        headers=actor_headers(member['id']),
    )
    assert filtered.status_code == 200
    ids = [task['id'] for group in filtered.json()['groups'] for task in group['tasks']]
    assert t1['id'] in ids
    assert t2['id'] not in ids

    reorder = client.patch('/tasks/reorder', json={'task_ids': [t2['id'], t1['id']]}, headers=actor_headers(member['id']))
    assert reorder.status_code == 204

    read_t1 = client.get(f"/tasks/{t1['id']}", headers=actor_headers(member['id'])).json()
    read_t2 = client.get(f"/tasks/{t2['id']}", headers=actor_headers(member['id'])).json()
    assert read_t2['list_order'] < read_t1['list_order']


def test_subtask_crud(client) -> None:
    users = client.get('/users').json()
    admin, member, _ = select_users(users)

    task = client.post(
        '/tasks',
        json={
            'title': 'Fix listing SEO',
            'assigned_to': member['id'],
            'created_by': admin['id'],
        },
        headers=actor_headers(admin['id']),
    ).json()

    create = client.post(
        f"/tasks/{task['id']}/subtasks",
        json={'content': 'Audit keywords', 'position': 1},
        headers=actor_headers(member['id']),
    )
    assert create.status_code == 201
    subtask_id = create.json()['id']

    update = client.patch(
        f"/tasks/{task['id']}/subtasks/{subtask_id}",
        json={'is_done': True},
        headers=actor_headers(member['id']),
    )
    assert update.status_code == 200
    assert update.json()['is_done'] is True

    all_subtasks = client.get(f"/tasks/{task['id']}/subtasks", headers=actor_headers(member['id']))
    assert all_subtasks.status_code == 200
    assert len(all_subtasks.json()) == 1

    delete = client.delete(
        f"/tasks/{task['id']}/subtasks/{subtask_id}",
        headers=actor_headers(member['id']),
    )
    assert delete.status_code == 204

    all_subtasks_after = client.get(f"/tasks/{task['id']}/subtasks", headers=actor_headers(member['id']))
    assert all_subtasks_after.status_code == 200
    assert all_subtasks_after.json() == []


def test_comment_crud(client) -> None:
    users = client.get('/users').json()
    admin, member, _ = select_users(users)

    task = client.post(
        '/tasks',
        json={
            'title': 'Comment-ready task',
            'assigned_to': member['id'],
            'created_by': admin['id'],
        },
        headers=actor_headers(admin['id']),
    ).json()

    create = client.post(
        f"/tasks/{task['id']}/comments",
        json={'content': 'Please review @Quang', 'mentions': ['@Quang'], 'author_id': member['id']},
        headers=actor_headers(member['id']),
    )
    assert create.status_code == 201
    comment_id = create.json()['id']
    assert create.json()['content'] == 'Please review @Quang'
    assert create.json()['mentions'] == ['@Quang']
    assert create.json()['author_id'] == member['id']

    all_comments = client.get(f"/tasks/{task['id']}/comments", headers=actor_headers(member['id']))
    assert all_comments.status_code == 200
    assert len(all_comments.json()) == 1

    delete = client.delete(f"/tasks/{task['id']}/comments/{comment_id}", headers=actor_headers(member['id']))
    assert delete.status_code == 204

    all_comments_after = client.get(f"/tasks/{task['id']}/comments", headers=actor_headers(member['id']))
    assert all_comments_after.status_code == 200
    assert all_comments_after.json() == []


def test_attachment_crud_and_size_limit(client, monkeypatch) -> None:
    users = client.get('/users').json()
    admin, member, _ = select_users(users)
    monkeypatch.setattr('app.main.MAX_ATTACHMENT_BYTES', 10)

    task = client.post(
        '/tasks',
        json={
            'title': 'Attachment task',
            'assigned_to': member['id'],
            'created_by': admin['id'],
        },
        headers=actor_headers(admin['id']),
    ).json()

    create = client.post(
        f"/tasks/{task['id']}/attachments",
        headers=actor_headers(member['id']),
        data={'uploaded_by': member['id']},
        files={'file': ('logo.png', b'abcde', 'image/png')},
    )
    assert create.status_code == 201
    attachment_id = create.json()['id']
    assert create.json()['name'] == 'logo.png'
    assert create.json()['is_image'] is True

    too_big = client.post(
        f"/tasks/{task['id']}/attachments",
        headers=actor_headers(member['id']),
        data={'uploaded_by': member['id']},
        files={'file': ('big.bin', b'01234567890', 'application/octet-stream')},
    )
    assert too_big.status_code == 400

    all_attachments = client.get(f"/tasks/{task['id']}/attachments", headers=actor_headers(member['id']))
    assert all_attachments.status_code == 200
    assert len(all_attachments.json()) == 1

    delete = client.delete(
        f"/tasks/{task['id']}/attachments/{attachment_id}",
        headers=actor_headers(member['id']),
    )
    assert delete.status_code == 204

    all_attachments_after = client.get(f"/tasks/{task['id']}/attachments", headers=actor_headers(member['id']))
    assert all_attachments_after.status_code == 200
    assert all_attachments_after.json() == []


def test_member_only_sees_own_tasks(client) -> None:
    users = client.get('/users').json()
    admin, member_a, member_b = select_users(users)

    task_a = client.post(
        '/tasks',
        json={
            'title': 'Task A',
            'assigned_to': member_a['id'],
            'created_by': admin['id'],
            'due_date': date.today().isoformat(),
        },
        headers=actor_headers(admin['id']),
    )
    assert task_a.status_code == 201

    task_b = client.post(
        '/tasks',
        json={
            'title': 'Task B',
            'assigned_to': member_b['id'],
            'created_by': admin['id'],
            'due_date': date.today().isoformat(),
        },
        headers=actor_headers(admin['id']),
    )
    assert task_b.status_code == 201

    list_as_a = client.get('/tasks', params={'view': 'today'}, headers=actor_headers(member_a['id']))
    assert list_as_a.status_code == 200
    ids = {task['id'] for group in list_as_a.json()['groups'] for task in group['tasks']}
    assert task_a.json()['id'] in ids
    assert task_b.json()['id'] not in ids


def test_member_cannot_assign_or_approve(client) -> None:
    users = client.get('/users').json()
    admin, member_a, member_b = select_users(users)

    forbidden_assign = client.post(
        '/tasks',
        json={'title': 'Should fail assign', 'assigned_to': member_b['id']},
        headers=actor_headers(member_a['id']),
    )
    assert forbidden_assign.status_code == 403

    created = client.post(
        '/tasks',
        json={'title': 'Member own task'},
        headers=actor_headers(member_a['id']),
    )
    assert created.status_code == 201
    task_id = created.json()['id']

    forbidden_ready = client.patch(
        f'/tasks/{task_id}/status',
        json={'status': 'ready'},
        headers=actor_headers(member_a['id']),
    )
    assert forbidden_ready.status_code == 403

    # Admin can still approve if task reaches review.
    move_to_review = client.patch(
        f'/tasks/{task_id}/status',
        json={'status': 'review'},
        headers=actor_headers(member_a['id']),
    )
    assert move_to_review.status_code == 200

    approve = client.patch(
        f'/tasks/{task_id}/status',
        json={'status': 'ready'},
        headers=actor_headers(admin['id']),
    )
    assert approve.status_code == 200
    assert approve.json()['status'] == 'ready'

    member_done = client.patch(
        f'/tasks/{task_id}/status',
        json={'status': 'done'},
        headers=actor_headers(member_a['id']),
    )
    assert member_done.status_code == 200
    assert member_done.json()['status'] == 'done'


def test_review_queue_access_and_approval_rule(client) -> None:
    users = client.get('/users').json()
    admin, member, _ = select_users(users)

    review_task = client.post(
        '/tasks',
        json={'title': 'Needs approval', 'assigned_to': member['id'], 'status': 'review'},
        headers=actor_headers(admin['id']),
    )
    assert review_task.status_code == 201
    review_task_id = review_task.json()['id']

    queue = client.get('/tasks', params={'view': 'review'}, headers=actor_headers(admin['id']))
    assert queue.status_code == 200
    queue_ids = [task['id'] for group in queue.json()['groups'] for task in group['tasks']]
    assert review_task_id in queue_ids

    member_queue = client.get('/tasks', params={'view': 'review'}, headers=actor_headers(member['id']))
    assert member_queue.status_code == 403

    todo_task = client.post(
        '/tasks',
        json={'title': 'Todo cannot be approved directly', 'assigned_to': member['id'], 'status': 'todo'},
        headers=actor_headers(admin['id']),
    )
    assert todo_task.status_code == 201
    todo_task_id = todo_task.json()['id']

    invalid_approve = client.patch(
        f'/tasks/{todo_task_id}/status',
        json={'status': 'ready'},
        headers=actor_headers(admin['id']),
    )
    assert invalid_approve.status_code == 400


def test_task_type_management_admin_only(client) -> None:
    users = client.get('/users').json()
    admin, member, _ = select_users(users)

    create_forbidden = client.post(
        '/task-types',
        json={'name': 'Ops'},
        headers=actor_headers(member['id']),
    )
    assert create_forbidden.status_code == 403

    create_ok = client.post(
        '/task-types',
        json={'name': 'Ops'},
        headers=actor_headers(admin['id']),
    )
    assert create_ok.status_code == 201
    created_type = create_ok.json()

    duplicate = client.post(
        '/task-types',
        json={'name': 'ops'},
        headers=actor_headers(admin['id']),
    )
    assert duplicate.status_code == 409

    delete_forbidden = client.delete(
        f"/task-types/{created_type['id']}",
        headers=actor_headers(member['id']),
    )
    assert delete_forbidden.status_code == 403

    delete_ok = client.delete(
        f"/task-types/{created_type['id']}",
        headers=actor_headers(admin['id']),
    )
    assert delete_ok.status_code == 204


def test_shop_and_task_type_edit_management_admin_only(client) -> None:
    users = client.get('/users').json()
    admin, member, _ = select_users(users)

    create_shop_forbidden = client.post(
        '/shops',
        json={'name': 'New Shop'},
        headers=actor_headers(member['id']),
    )
    assert create_shop_forbidden.status_code == 403

    create_shop_ok = client.post(
        '/shops',
        json={'name': 'New Shop'},
        headers=actor_headers(admin['id']),
    )
    assert create_shop_ok.status_code == 201
    created_shop_id = create_shop_ok.json()['id']

    update_shop_forbidden = client.patch(
        f'/shops/{created_shop_id}',
        json={'name': 'New Shop Edited'},
        headers=actor_headers(member['id']),
    )
    assert update_shop_forbidden.status_code == 403

    update_shop_ok = client.patch(
        f'/shops/{created_shop_id}',
        json={'name': 'New Shop Edited'},
        headers=actor_headers(admin['id']),
    )
    assert update_shop_ok.status_code == 200
    assert update_shop_ok.json()['name'] == 'New Shop Edited'

    delete_shop_ok = client.delete(
        f'/shops/{created_shop_id}',
        headers=actor_headers(admin['id']),
    )
    assert delete_shop_ok.status_code == 204

    task_types = client.get('/task-types').json()
    assert task_types
    target_type_id = task_types[0]['id']

    update_type_forbidden = client.patch(
        f'/task-types/{target_type_id}',
        json={'name': 'Edited by member'},
        headers=actor_headers(member['id']),
    )
    assert update_type_forbidden.status_code == 403

    update_type_ok = client.patch(
        f'/task-types/{target_type_id}',
        json={'name': 'Edited by admin'},
        headers=actor_headers(admin['id']),
    )
    assert update_type_ok.status_code == 200
    assert update_type_ok.json()['name'] == 'Edited by admin'
