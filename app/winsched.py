"""The daily sync as a Windows Task Scheduler task for the current user.

Runs `pythonw -m app.daily_sync` (no window) at the chosen time. StartWhenAvailable makes Windows
run it later if the computer was off or asleep at that time.
"""
import json
import subprocess
from pathlib import Path

TASK_NAME = "SmartSchool-DailySync"
ROOT = Path(__file__).resolve().parent.parent
PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"


def _ps(script: str) -> str:
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or out.stdout.strip())
    return out.stdout.strip()


def register(at: str):
    """Create or update the task to run every day at HH:MM."""
    hh, mm = (int(x) for x in at.split(":"))
    _ps(f"""
$a = New-ScheduledTaskAction -Execute '{PYTHONW}' -Argument '-m app.daily_sync' -WorkingDirectory '{ROOT}'
$t = New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.AddHours({hh}).AddMinutes({mm}))
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t -Settings $s -Description 'Smart School daily homework sync' -Force | Out-Null
""")


def unregister():
    _ps(f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false -ErrorAction SilentlyContinue")


def status():
    """{'next': 'YYYY-MM-DD HH:MM', 'last': ..., 'last_result': int} or None if there's no task."""
    try:
        raw = _ps(f"""
$t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
if ($t) {{ $i = $t | Get-ScheduledTaskInfo
  @{{ next = if ($i.NextRunTime) {{ $i.NextRunTime.ToString('yyyy-MM-dd HH:mm') }} else {{ $null }};
      last = if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) {{ $i.LastRunTime.ToString('yyyy-MM-dd HH:mm') }} else {{ $null }};
      last_result = $i.LastTaskResult; state = "$($t.State)" }} | ConvertTo-Json -Compress }}
""")
        return json.loads(raw) if raw else None
    except Exception:
        return None
