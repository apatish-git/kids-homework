import hashlib
import json
import re
from pathlib import Path

from playwright.sync_api import Page


class Source:
    key = ""
    label = ""
    # Names of the logged-in user as seen by the site, set by fetch() when the site exposes them.
    # Used to make sure a reused single-sign-on session belongs to the right child.
    identity: list[str] | None = None

    def start_login(self, page: Page):
        """Navigate so that we either end up logged in or on the MoE login page."""
        raise NotImplementedError

    def is_logged_in(self, page: Page) -> bool:
        raise NotImplementedError

    def fetch(self, page: Page, raw_dir: Path) -> list[dict]:
        """Return normalized tasks: external_id, title, subject, description, due_date,
        assigned_date, url, source_status, raw."""
        raise NotImplementedError


_NAME_KEY = re.compile(r"^(first_?name|last_?name|full_?name|display_?name|user_?name|student_?name|name|fname|lname)$", re.I)
_HEBREW = re.compile(r"[֐-׿]")

# Hebrew person names kept in the site's local/session storage (the logged-in user's profile).
STORAGE_NAMES_JS = r"""() => {
  const out = new Set(), KEY = /^(first_?name|last_?name|full_?name|display_?name|user_?name|student_?name|name|fname|lname)$/i,
        HEB = /[֐-׿]/;
  const walk = (o, d) => { if (!o || typeof o !== 'object' || d > 4) return;
    for (const [k, v] of Object.entries(o)) {
      if (typeof v === 'string' && KEY.test(k) && HEB.test(v) && v.length < 40) out.add(v.trim());
      else if (v && typeof v === 'object') walk(v, d + 1);
    } };
  for (const st of [localStorage, sessionStorage])
    for (let i = 0; i < st.length; i++) { try { walk(JSON.parse(st.getItem(st.key(i))), 0); } catch (e) {} }
  return [...out];
}"""


def names_in(obj, depth=0) -> list[str]:
    """Same as STORAGE_NAMES_JS, for a Python object (e.g. a session returned by the site)."""
    out = []
    if isinstance(obj, dict) and depth < 5:
        for k, v in obj.items():
            if isinstance(v, str) and _NAME_KEY.match(k) and _HEBREW.search(v) and len(v) < 40:
                out.append(v.strip())
            elif isinstance(v, (dict, list)):
                out += names_in(v, depth + 1)
    elif isinstance(obj, list) and depth < 5:
        for v in obj:
            out += names_in(v, depth + 1)
    return out


def stable_id(*parts) -> str:
    return hashlib.sha1("|".join(str(p or "") for p in parts).encode("utf8")).hexdigest()[:20]


def dump(raw_dir: Path, name: str, data):
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf8")


_CATEGORIES = (
    ("homework", ("שיעורי בית", "ש.ב", "ש\"ב", "משימה", "מטלה", "הגשה")),
    ("grade", ("ציון", "הערכה", "מבחן", "בוחן")),
    ("attendance", ("חיסור", "איחור", "נוכחות", "אירוע משמעת", "הערת משמעת", "התנהגות")),
)
CATEGORY_LABELS = {"homework": "שיעורי בית", "grade": "ציונים", "attendance": "אירועים בשיעור", "other": "כללי"}


def notice_category(text: str) -> str:
    return next((cat for cat, words in _CATEGORIES if any(w in text for w in words)), "other")


def first(d: dict, *keys):
    """First non-empty value among keys (case-insensitive)."""
    lower = {k.lower(): v for k, v in d.items()}
    for k in keys:
        v = lower.get(k.lower())
        if v not in (None, "", [], {}):
            return v
    return None
