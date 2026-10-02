"""Galim Pro (גלים פרו, Snunit) — student tasks.

Login goes through MoE SSO (userdata.galim.org.il/login_idm). The site's own "my tasks" list is
POST https://lms.galim.org.il/personal_api/tasks {page, limit, filterData{...}} with cookies, which
we call from inside the logged-in page, once for open tasks and once for completed ones.
"""
from pathlib import Path

from playwright.sync_api import Page

from ..dates import to_iso_date
from .base import STORAGE_NAMES_JS, Source, dump, first

SITE = "https://pro.galim.org.il"
MOE_ENTRY = "https://userdata.galim.org.il/login_idm?request_uri=https%3A%2F%2Fpro.galim.org.il%2F"
LMS = "https://lms.galim.org.il"

# The site's own "who am I" call: the user object has uid (> 0 when logged in), firstname_he, current_school_id.
USERDATA_URL = "https://userdata.galim.org.il/?userdata=1&roles=1&schoolsV2=1&classes=1&JWT=clickim&cm_permissions=753"
USER_JS = """async (url) => {
  try {
    const r = await fetch(url, {credentials: 'include'});
    if (!r.ok) return null;
    const d = await r.json();
    const find = (o, depth) => { if (!o || typeof o !== 'object' || depth > 3) return null;
      if ('uid' in o) return o; for (const v of Object.values(o)) { const f = find(v, depth + 1); if (f) return f; } return null; };
    const u = find(d, 0);
    return u ? {uid: u.uid, firstname: u.firstname_he || u.firstname || '', lastname: u.lastname_he || u.lastname || '',
                school: u.current_school_id || (u.schoolsV2 && u.schoolsV2[0] && u.schoolsV2[0].id) || null} : null;
  } catch (e) { return null; }
}"""

FETCH_JS = """async ({lms, completed, schoolId}) => {
  const filterData = {completedActive: completed, selfActive: false, orderBy: 'assignDateOrder', class: -1,
    field: -1, checkType: -1, unit_type: -1, grade_display: -1, targetDateOrder: false,
    newSubmittedOrder: false, assignDateOrder: 1, nameOrder: false};
  if (schoolId) filterData.schoolId = schoolId;
  const r = await fetch(lms + '/personal_api/tasks', {method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({page: 0, limit: 200, filterData})});
  if (!r.ok) return {error: r.status};
  return await r.json();
}"""


def _task_list(data):
    """The response shape isn't documented; find the list of task objects in it."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("tasks", "data", "items", "results", "list"):
            if isinstance(data.get(key), list):
                return data[key]
            if isinstance(data.get(key), dict):
                found = _task_list(data[key])
                if found:
                    return found
    return []


class Galim(Source):
    key = "galim"
    label = "גלים פרו"

    def _user(self, page: Page):
        try:
            return page.evaluate(USER_JS, USERDATA_URL)
        except Exception:
            return None

    def is_logged_in(self, page: Page) -> bool:
        if "pro.galim.org.il" not in page.url:
            return False
        user = self._user(page)
        return bool(user and isinstance(user.get("uid"), (int, float)) and user["uid"] > 0)

    def start_login(self, page: Page):
        page.goto(SITE, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        if not self.is_logged_in(page):
            page.goto(MOE_ENTRY, wait_until="domcontentloaded")

    def fetch(self, page: Page, raw_dir: Path) -> list[dict]:
        user = self._user(page) or {}
        name = f"{user.get('firstname', '')} {user.get('lastname', '')}".strip()
        self.identity = [name] if name else page.evaluate(STORAGE_NAMES_JS)
        items = {}
        for completed in (False, True):
            data = page.evaluate(FETCH_JS, {"lms": LMS, "completed": completed, "schoolId": user.get("school")})
            dump(raw_dir, f"tasks_{'completed' if completed else 'open'}", data)
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(f"גלים פרו החזיר שגיאה {data['error']}")
            for t in _task_list(data):
                if isinstance(t, dict):
                    for item in self._normalize(t, completed):
                        items[(item.get("kind", "task"), item["external_id"])] = item
        return list(items.values())

    def _normalize(self, t: dict, completed: bool) -> list[dict]:
        unit = t.get("unit") if isinstance(t.get("unit"), dict) else {}
        task_id = str(first(t, "_id", "id", "task_id", "assignment_id") or first(unit, "_id", "id"))
        title = first(t, "name", "title", "task_name") or first(unit, "name", "title") or "משימה בגלים פרו"
        subject = first(t, "field_name", "fieldName", "subject") or first(unit, "field_name", "field")
        score = first(t, "grade", "score", "final_grade")
        status = first(t, "status", "state")
        task = {
            "external_id": task_id,
            "title": str(title)[:200],
            "subject": subject if isinstance(subject, str) else None,
            "description": first(t, "description", "instructions", "teacher_message", "message"),
            "due_date": to_iso_date(first(t, "target_date", "targetDate", "due_date", "end_date")),
            "assigned_date": to_iso_date(first(t, "assign_date", "assignDate", "start_date", "created")),
            "url": first(t, "url", "link") or SITE,
            "source_status": "הוגש" if completed else (status if isinstance(status, str) else None),
            "raw": t,
        }
        items = [task]
        if score not in (None, "", -1):
            items.append({**task, "kind": "grade", "score": score, "due_date": None,
                          "assigned_date": to_iso_date(first(t, "submit_date", "submitted_at", "grade_date"))
                          or task["due_date"]})
        return items
