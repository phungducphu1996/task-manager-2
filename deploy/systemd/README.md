# Task Manager Systemd Timers

Use `systemd timer` on the VPS instead of root crontab for reminder and Gmail monitor jobs.

## Install Reminder Tick

```bash
cd /opt/task-manager
bash deploy/systemd/install-reminder-timer.sh
```

## Install Gmail Monitor

```bash
cd /opt/task-manager
bash deploy/systemd/install-gmail-timers.sh
```

This installs:
- `taskmanager-gmail-poll.timer`: calls `/internal/gmail/poll` every 2 minutes.
- `taskmanager-gmail-digest.timer`: checks `/internal/gmail/digest` every 5 minutes; the backend sends once daily at the time configured in the Gmail/Zalo UI.

## Verify Reminder

```bash
systemctl status taskmanager-reminder-tick.timer --no-pager
systemctl list-timers --all | grep taskmanager-reminder-tick
journalctl -u taskmanager-reminder-tick.service -n 50 --no-pager
```

## Verify Gmail Monitor

```bash
systemctl status taskmanager-gmail-poll.timer --no-pager
systemctl status taskmanager-gmail-digest.timer --no-pager
systemctl list-timers --all | grep taskmanager-gmail
journalctl -u taskmanager-gmail-poll.service -n 50 --no-pager
journalctl -u taskmanager-gmail-digest.service -n 50 --no-pager
```

## Manual Test

```bash
/opt/task-manager/deploy/systemd/run-reminder-tick.sh
/opt/task-manager/deploy/systemd/run-gmail-poll.sh
/opt/task-manager/deploy/systemd/run-gmail-digest.sh
```

## Important

Remove old crontab entries for `/internal/reminders/tick`, `/internal/gmail/poll`, or `/internal/gmail/digest` after the timers are healthy, otherwise notifications may be duplicated.
