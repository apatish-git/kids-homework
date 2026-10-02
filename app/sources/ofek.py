"""Ofek (CET / מטח) — student tasks from the dashboard micro-service.

After login the site exposes window.cet.accessmanagement (session) and
window.cet.microservices.dashboardapi.students.getTasks({studentId, schoolId, minDate, maxDate}),
which is exactly what the site's own dashboard calls. We call it from inside the page.
"""
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import Page

from ..dates import to_iso_date
from ..moe_login import StopLogin
from .base import STORAGE_NAMES_JS, Source, dump, first, names_in

SITE = "https://myofek.cet.ac.il/he"
DASHBOARD_JS = "https://nairobigateway.cet.ac.il/dashboardapi/provider/dashboardprovider.js"
DONE_STATUSES = {"submitted", "evaluated", "served", "completed", "done", "checked"}
STATUS_LABELS = {"new": "חדש", "assigned": "חדש", "started": "בתהליך", "inprogress": "בתהליך", "returned": "הוחזר לתיקון"}
DISCIPLINES = {"lashon": "עברית", "hebrew": "עברית", "math": "מתמטיקה", "mathematics": "מתמטיקה",
               "english": "אנגלית", "science": "מדע וטכנולוגיה", "moledet": "מולדת", "geography": "גאוגרפיה",
               "history": "היסטוריה", "tanach": "תנ\"ך", "bible": "תנ\"ך", "arabic": "ערבית", "literature": "ספרות",
               "civics": "אזרחות", "lifeskills": "כישורי חיים"}

SCHOOL_PICKER_TITLE = "בחירת בית ספר"
SCHOOL_PICKER_PLACEHOLDER = "בתי ספר לבחירה"


class SchoolNotChosen(StopLogin):
    """CET asked which school to use and no school is configured for this child yet."""


SESSION_JS ="""async () => {
  if (!window.cet || !window.cet.accessmanagement) return null;
  const s = await window.cet.accessmanagement.getSessionAsync();
  return s && s.userId && (s.role || '').toLowerCase() !== 'guest' ? {userId: s.userId, role: s.role, schoolId: s.schoolId} : null;
}"""

FETCH_JS = """async ({minDate, maxDate, dashboardJs}) => {
  const s = await window.cet.accessmanagement.getSessionAsync();
  if (!(window.cet.microservices && window.cet.microservices.dashboardapi)) {
    await new Promise((res, rej) => { const el = document.createElement('script'); el.src = dashboardJs;
      el.onload = res; el.onerror = rej; document.head.appendChild(el); });
  }
  const req = {studentId: String(s.userId).toLowerCase(), minDate, maxDate};
  if (s.schoolId) req.schoolId = s.schoolId;
  const tasks = await window.cet.microservices.dashboardapi.students.getTasks(req);
  return {role: s.role, session: s, tasks: tasks || []};
}"""


class Ofek(Source):
    key = "ofek"
    label = "אופק"

    # Set by sync before each login (single worker thread).
    school = ""             # the school to pick when CET asks "בחירת בית ספר"
    interactive = False
    school_options = None   # filled when the picker showed up and we didn't know which school to pick

    def configure(self, child: dict, interactive: bool):
        self.school = (child.get("ofek_school") or "").strip()
        self.interactive = interactive
        self.school_options = None

    def _handle_school_picker(self, page: Page):
        """Students linked to several schools get a 'בחירת בית ספר' dialog right after login."""
        try:
            if not page.get_by_text(SCHOOL_PICKER_TITLE).first.is_visible():
                return
        except Exception:
            return
        if self.interactive and not self.school:
            return   # the parent is picking in the visible window
        if self.school_options is not None and not self.school:
            return   # already collected the list, waiting for the parent to choose in settings

        native = page.locator("select:visible")
        if native.count():
            options = [o.strip() for o in native.first.locator("option").all_inner_texts()]
        else:
            page.get_by_text(SCHOOL_PICKER_PLACEHOLDER).first.click()
            page.wait_for_timeout(800)
            options = [o.strip() for o in page.get_by_role("option").all_inner_texts()]
            if not options:
                options = [o.strip() for o in page.locator("[role=listbox] li, ul[class*=list] li").all_inner_texts()]
        options = [o for o in options if o and SCHOOL_PICKER_PLACEHOLDER not in o]

        match = next((o for o in options if self.school and (self.school in o or o in self.school)), None)
        if not match:
            self.school_options = options
            page.keyboard.press("Escape")
            return
        if native.count():
            native.first.select_option(label=match)
        else:
            page.get_by_text(match, exact=True).first.click()
        page.wait_for_timeout(500)
        page.get_by_role("button", name="המשך").first.click()

    def is_logged_in(self, page: Page) -> bool:
        self._handle_school_picker(page)
        if self.school_options is not None and not self.interactive:
            raise SchoolNotChosen()
        if "myofek.cet.ac.il" not in page.url:
            return False
        try:
            return page.evaluate(SESSION_JS) is not None
        except Exception:
            return False

    def start_login(self, page: Page):
        page.goto(SITE, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        if self.is_logged_in(page):
            return
        page.get_by_text("להתחברות").first.click()
        page.get_by_text("התחברות משרד החינוך").first.click(timeout=10000)

    def fetch(self, page: Page, raw_dir: Path) -> list[dict]:
        today = date.today()
        args = {
            "minDate": (today - timedelta(days=60)).strftime("%a %b %d %Y"),
            "maxDate": (today + timedelta(days=180)).strftime("%a %b %d %Y"),
            "dashboardJs": DASHBOARD_JS,
        }
        result = page.evaluate(FETCH_JS, args)
        self.identity = names_in(result.pop("session", None)) + page.evaluate(STORAGE_NAMES_JS)
        dump(raw_dir, "tasks", result)
        if (result.get("role") or "").lower() not in ("student", ""):
            raise RuntimeError(f"המשתמש מחובר באופק בתפקיד '{result.get('role')}' ולא כתלמיד")
        items = []
        for t in result["tasks"]:
            if not isinstance(t, dict):
                continue
            task = self._normalize(t)
            items.append(task)
            if t.get("taskScore") is not None and not t.get("hideGrade"):
                items.append({**task, "kind": "grade", "score": t["taskScore"], "due_date": None,
                              "assigned_date": to_iso_date(first(t, "taskStatusDate", "taskSubmitDate")) or task["due_date"]})
        return items

    def _normalize(self, t: dict) -> dict:
        task_id = str(first(t, "taskId", "id"))
        status = first(t, "taskStatus", "studentStatus", "status", "submissionStatus")
        status = status if isinstance(status, str) else None
        if status and status.lower() in DONE_STATUSES:
            label = "הוגש"
            if t.get("taskScore") is not None and not t.get("hideGrade"):
                label += f" · ציון {t['taskScore']}"
        else:
            label = STATUS_LABELS.get((status or "").lower(), status)
        disciplines = [DISCIPLINES.get(d, d) for d in (t.get("taskDisciplines") or []) if isinstance(d, str)]
        teacher = t.get("taskTeacherName")
        return {
            "external_id": task_id,
            "title": str(first(t, "taskName", "title", "name", "itemName") or "משימה באופק")[:200],
            "subject": ", ".join(disciplines) or first(t, "itemParentName", "subjectName"),
            "description": " · ".join(x for x in (t.get("itemParentName"), f"מורה: {teacher}" if teacher else None) if x) or None,
            "due_date": to_iso_date(first(t, "taskDueDate", "dueDate")),
            "assigned_date": to_iso_date(first(t, "taskStartDate", "taskCreationDate")),
            "url": f"{SITE}/task/{task_id}",
            "source_status": label,
            "raw": t,
        }
