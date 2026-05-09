# Task Manager Reminder Timer

Use `systemd timer` on the VPS instead of root crontab for reminder ticks.

## Install

```bash
cd /opt/task-manager
bash deploy/systemd/install-reminder-timer.sh
```

## Verify

```bash
systemctl status taskmanager-reminder-tick.timer --no-pager
systemctl list-timers --all | grep taskmanager-reminder-tick
journalctl -u taskmanager-reminder-tick.service -n 50 --no-pager
```

## Manual test

```bash
/opt/task-manager/deploy/systemd/run-reminder-tick.sh
```

## Important

Remove the old crontab entry for `/internal/reminders/tick` after the timer is healthy, otherwise notifications may be duplicated.
