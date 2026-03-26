# UI Optimization Execution Checklist (v1)

This checklist translates the UI feedback into implementable tasks for the current codebase, without rebuilding architecture.

## Scope

- Keep existing stack and layout: `Sidebar / List / Detail`.
- Keep existing statuses in DB: `todo | doing | review | ready | done`.
- Focus on scan speed, execution speed, and team clarity.

## P0 Stability (Do First)

1. Verify task click always opens detail panel.
- Files: `frontend/src/components/TaskRow.vue`, `frontend/src/components/TaskBoard.vue`, `frontend/src/stores/taskStore.ts`, `frontend/src/assets/styles.css`
- Done when:
  - Clicking task row selects it reliably.
  - On narrow screens, panel is visible immediately (auto-scroll behavior).
  - No row area is blocked by hidden action layer.

2. Add regression test for “select -> detail visible state”.
- Files: `frontend/tests/TaskList.spec.ts` (or new `TaskBoard.spec.ts`)
- Done when:
  - Test asserts select event and selected styling/state update.

## P1 UX Improvements (Top 3)

1. Inbox grouping: `Overdue / Today / Review / Inbox`.
- Files: `backend/app/services.py`, `backend/app/schemas.py` (only if extra fields needed), `frontend/src/components/TaskList.vue`
- Backend logic order (exclusive buckets):
  - `Overdue`: `due_date < today`
  - `Today`: `due_date == today` or `scheduled_date == today`
  - `Review`: `status == review`
  - `Inbox`: remaining items in inbox query
- Done when:
  - `GET /tasks?view=inbox` returns grouped sections in that order.
  - Each task appears in exactly one group.

2. Improve task row metadata readability.
- Files: `frontend/src/components/TaskRow.vue`, `frontend/src/assets/styles.css`
- Changes:
  - Show assignee (`Unassigned` fallback).
  - Show status chip.
  - Keep shop/type/date in lightweight meta line.
- Done when:
  - User can identify owner + status in one glance.
  - Row remains clean, no multi-line clutter on desktop.

3. Status/overdue highlighting with subtle colors.
- Files: `frontend/src/components/TaskRow.vue`, `frontend/src/assets/styles.css`
- Style mapping:
  - `review`: soft yellow chip
  - `doing`: soft blue chip
  - overdue row: soft red tint/border
- Done when:
  - Status is visually scannable without reading full text.
  - Color usage stays minimal and consistent.

## P2 Interaction Speed

1. Quick-add parser in input (`#shop @assignee !priority today`).
- Files: `frontend/src/components/TaskBoard.vue`, `frontend/src/stores/taskStore.ts`
- Parsing rules (safe fallback):
  - `#...` -> match shop by name (case-insensitive)
  - `@...` -> match assignee by display name
  - `!high|!medium|!low|!urgent` -> priority
  - `today` -> `scheduled_date=today`
  - Unknown tokens remain in title; never block creation
- Done when:
  - One-line command creates task with parsed fields.
  - Invalid token never causes failed create.

2. Safer delete behavior.
- Files: `frontend/src/components/TaskRow.vue`, `frontend/src/components/TaskDetailPanel.vue`
- Changes:
  - Keep delete hidden until hover/selection in list.
  - Optional: move destructive delete confirmation into detail panel.
- Done when:
  - No accidental delete from casual row click.

3. Keyboard boosts.
- Files: `frontend/src/components/TaskBoard.vue`, `frontend/src/components/TaskDetailPanel.vue`
- Shortcuts:
  - `Enter` in quick-add creates task.
  - `Space` toggles selected task done (when focus not inside input/textarea).
  - `Enter` in detail title triggers save.
- Done when:
  - Core daily flow works with keyboard only.

## P3 Detail Panel Tone (Less “Admin Form”)

1. Reduce heavy form feel.
- Files: `frontend/src/components/TaskDetailPanel.vue`, `frontend/src/assets/styles.css`
- Changes:
  - Smaller labels, softer borders.
  - Better spacing rhythm and section hierarchy.
  - Keep editable fields but present as “inline editing”.
- Done when:
  - Detail panel feels lightweight and execution-focused.

## Test Plan

1. Backend tests.
- File: `backend/tests/test_task_views.py` (extend)
- Add:
  - Inbox grouping order and exclusivity.
  - Overdue/today/review edge cases.

2. Frontend tests.
- Files: `frontend/tests/TaskRow.spec.ts`, `frontend/tests/TaskList.spec.ts`, optional `frontend/tests/TaskBoard.spec.ts`
- Add:
  - Row metadata render (assignee/status).
  - Status class/chip rendering.
  - Click-to-select stability.

3. Manual acceptance (happy path).
- Open `/today`, click task -> detail opens instantly.
- Open `/inbox`, verify groups in expected order.
- Quick-add with `#shop @assignee !high today`, verify fields populated.

## Rollout Order (Recommended)

1. P0 Stability
2. P1 Inbox grouping + row readability + status colors
3. P2 quick-add parser + delete safety + keyboard
4. P3 detail panel polish

