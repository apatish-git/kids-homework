import re
from datetime import datetime, timezone

_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
            "%d/%m/%Y", "%d.%m.%Y", "%d/%m/%y", "%a %b %d %Y", "%m/%d/%Y %H:%M:%S")


def to_iso_date(value):
    """Best-effort conversion of the many date shapes these sites return to YYYY-MM-DD."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        ts = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().date().isoformat()
    s = str(value).strip()
    m = re.match(r"/Date\((\d+)", s)
    if m:
        return to_iso_date(int(m.group(1)))
    if re.search(r"T\d\d:\d\d.*(Z|[+-]\d\d:?\d\d)$", s):
        try:   # timezone-aware: convert to local (Israel) date
            return datetime.fromisoformat(s).astimezone().date().isoformat()
        except ValueError:
            pass
    s = re.sub(r"(\.\d+)?(Z|[+-]\d\d:?\d\d)$", "", s)
    for fmt in _FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return "-".join(m.groups())
    return None
