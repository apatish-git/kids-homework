"""The daily sync, scheduled by the operating system so it runs even when the web app is closed.

- Windows: a Task Scheduler task for the current user (`pythonw -m app.daily_sync`, no window).
  StartWhenAvailable runs it later if the computer was off or asleep at that time.
- Linux: one line in the user's crontab, tagged with a marker comment so we only ever touch
  our own line.
"""
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WINDOWS = sys.platform.startswith("win")
TASK_NAME = "SmartSchool-DailySync"           # Windows task
CRON_MARK = "# smart-school-daily-sync"       # Linux crontab line marker


def system_name() -> str:
    return "מתזמן המשימות של Windows" if WINDOWS else "cron"


# ---------- Windows ----------

def _ps(script: str) -> str:
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or out.stdout.strip())
    return out.stdout.strip()


def _win_register(hh: int, mm: int):
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    _ps(f"""
$a = New-ScheduledTaskAction -Execute '{pythonw}' -Argument '-m app.daily_sync' -WorkingDirectory '{ROOT}'
$t = New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.AddHours({hh}).AddMinutes({mm}))
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t -Settings $s -Description 'Smart School daily homework sync' -Force | Out-Null
""")


def _win_unregister():
    _ps(f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false -ErrorAction SilentlyContinue")


def _win_status():
    raw = _ps(f"""
$t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
if ($t) {{ $i = $t | Get-ScheduledTaskInfo
  @{{ next = if ($i.NextRunTime) {{ $i.NextRunTime.ToString('yyyy-MM-dd HH:mm') }} else {{ $null }};
      last = if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) {{ $i.LastRunTime.ToString('yyyy-MM-dd HH:mm') }} else {{ $null }} }} | ConvertTo-Json -Compress }}
""")
    return json.loads(raw) if raw else None


# ---------- Linux (cron) ----------

def _crontab_lines() -> list[str]:
    if not shutil.which("crontab"):
        raise RuntimeError("cron לא מותקן (sudo apt install cron)")
    out = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return out.stdout.splitlines() if out.returncode == 0 else []   # no crontab yet -> empty


def _crontab_write(lines: list[str]):
    out = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())


def _cron_register(hh: int, mm: int):
    python = ROOT / ".venv" / "bin" / "python"
    line = f'{mm} {hh} * * * cd "{ROOT}" && "{python}" -m app.daily_sync >/dev/null 2>&1 {CRON_MARK}'
    _crontab_write([l for l in _crontab_lines() if CRON_MARK not in l] + [line])


def _cron_unregister():
    lines = _crontab_lines()
    if any(CRON_MARK in l for l in lines):
        _crontab_write([l for l in lines if CRON_MARK not in l])


def _cron_status():
    line = next((l for l in _crontab_lines() if CRON_MARK in l), None)
    if not line:
        return None
    mm, hh = (int(x) for x in line.split()[:2])
    now = datetime.now()
    nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    last = None
    log = ROOT / "data" / "daily_sync.log"
    if log.exists():
        tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        last = tail[-1][:16] if tail else None
    return {"next": nxt.strftime("%Y-%m-%d %H:%M"), "last": last}


# ---------- public ----------

def register(at: str):
    """Run the daily sync every day at HH:MM (creates or updates the schedule)."""
    hh, mm = (int(x) for x in at.split(":"))
    (_win_register if WINDOWS else _cron_register)(hh, mm)


def unregister():
    (_win_unregister if WINDOWS else _cron_unregister)()


def status():
    """{'next': 'YYYY-MM-DD HH:MM', 'last': ...} or None if not scheduled."""
    try:
        return _win_status() if WINDOWS else _cron_status()
    except Exception:
        return None
