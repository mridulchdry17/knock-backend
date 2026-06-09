# Launch runbook — flip uvicorn to systemd, deploy code

Procedure for cutting over from the manually-running uvicorn (tmux/pts session)
to a systemd-managed service. Also covers routine deploys once systemd owns it.

**Audience:** SSH into the production VM as `azureuser`.

---

## One-time activation: flip from manual uvicorn to systemd

Run these in order. Each step is independently verifiable; stop and investigate
if any step fails before continuing.

### 1. Confirm current health
```bash
curl -s http://localhost:8000/healthz
```
Expect `{"status":"ok"}`. If not, fix that before proceeding — don't stack a
systemd cutover on top of a sick service.

### 2. Pull latest code
```bash
cd /home/azureuser/outreach-backend
git fetch --all
git checkout main
git pull --ff-only origin main
```

### 3. Install any new dependencies (idempotent)
```bash
.venv/bin/pip install -e .
```

### 4. Run pending migrations
```bash
.venv/bin/alembic upgrade head
```
On Turso this is a no-op if the schema is already current. Safe to re-run.

### 5. Copy the systemd unit file
```bash
sudo cp deploy/systemd/knock-api.service /etc/systemd/system/knock-api.service
sudo systemctl daemon-reload
```

### 6. Find and stop the manually-running uvicorn
```bash
# Identify the manual uvicorn (started from a pts session, not systemd)
ps -ef | grep -E "uvicorn.*app.main:app" | grep -v grep
```
Note the PID. If it's running inside tmux, attach and exit cleanly:
```bash
tmux ls                                    # find the session name
tmux attach -t <session-name>              # in the tmux pane: Ctrl+C, then exit
```
Or kill directly:
```bash
sudo kill <PID>
# wait 2 seconds, then verify:
curl -s http://localhost:8000/healthz      # should fail (Connection refused)
```

### 7. Start systemd-managed uvicorn
```bash
sudo systemctl enable --now knock-api
sudo systemctl status knock-api
```
Expect `active (running)`.

### 8. Verify
```bash
curl -s http://localhost:8000/healthz                       # local probe
curl -s https://knock-api.koreacentral.cloudapp.azure.com/healthz   # via Caddy
sudo journalctl -u knock-api -n 30                          # last 30 log lines
```

Submit a real test email on the live frontend. Confirm the row lands in Turso.

### 9. Reboot test (optional but recommended)
```bash
sudo reboot
```
After ~1 minute, SSH back in and verify uvicorn auto-started:
```bash
sudo systemctl status knock-api
curl -s http://localhost:8000/healthz
```
If healthy → systemd is working. If not → something's wrong with the unit file
or the EnvironmentFile path; check `journalctl -u knock-api`.

---

## Routine deploys (after systemd is active)

```bash
cd /home/azureuser/outreach-backend
git fetch --all
git checkout main
git pull --ff-only origin main
.venv/bin/pip install -e .                # only if pyproject changed
.venv/bin/alembic upgrade head            # only if new migrations
sudo systemctl restart knock-api
sudo journalctl -u knock-api -n 30 -f     # tail logs to confirm clean start
```

In-flight requests during the restart get a 502 from Caddy for ~1-2 seconds.
Acceptable for a single-VM v1; for v2+ we'd want graceful drain or a second VM.

---

## Rollback if something breaks

If the new service won't stay up:
```bash
sudo systemctl stop knock-api
# bring back the manual uvicorn the way it ran before:
cd /home/azureuser/outreach-backend
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 &
disown
```
Then debug the unit file separately.

If a code deploy broke production:
```bash
git log --oneline -5                       # find the previous good commit
git checkout <previous-sha>
sudo systemctl restart knock-api
```

---

## Troubleshooting

**`Failed to start knock-api.service: Unit not found`**
Did `daemon-reload` after copying the unit file? Run `sudo systemctl daemon-reload`.

**Service starts but immediately exits**
Check `journalctl -u knock-api -n 50`. Common causes:
- `EnvironmentFile` path wrong / file missing.
- `.venv/bin/uvicorn` doesn't exist (venv missing or in different location).
- Python errors at import time (syntax error, missing dependency).

**Service runs but `/healthz` returns connection refused**
uvicorn is bound to `127.0.0.1:8000`. From localhost on the VM, `curl localhost:8000/healthz` works. Caddy reaches it via localhost. If you SSH-tunnel and try to reach `:8000` directly from your laptop, you'll get refused — that's intentional defense-in-depth.

**TLS cert expired or renewal failing**
That's a Caddy concern, not knock-api. Check `sudo journalctl -u caddy -n 100 | grep -i acme`.
