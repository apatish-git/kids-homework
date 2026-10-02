"""Smartschool (Webtop) — homework from the student card, 'lessons & homework' module.

The web app calls POST https://webtopserver.smartschool.co.il/server/api/PupilCard/GetPupilLessonsAndHomework
(cookie auth) with {studentID, classCode, moduleID, weekIndex, ...}. We open the page once so the
app builds that request for us, then replay it for last/this/next week.
"""
import html
import json
import re
from pathlib import Path

from playwright.sync_api import Page

from ..dates import to_iso_date
from ..moe_login import LoginResult, _wait_for, type_into
from .base import Source, dump, first, notice_category, stable_id

SITE = "https://webtop.smartschool.co.il"
MOE_ENTRY = "https://www.webtop.co.il/applications/loginMOENew/default.aspx"
HOMEWORK_PAGE = f"{SITE}/Student_Card/11"
HW_ENDPOINT = "PupilCard/GetPupilLessonsAndHomework"
SETTINGS_ENDPOINT = "PupilCard/GetSettingsList"
GRADES_ENDPOINT = "PupilCard/GetPupilGrades"
NOTIFICATIONS_ENDPOINT = "Notification/GetNotificationList"
# Smartschool's notification moduleName -> our category
NOTICE_MODULES = {"lessonSubjectHomework": "homework", "grades": "grade", "periodGrades": "grade",
                  "evaluationComponent": "grade", "matriculationGrades": "grade", "lessonEvents": "attendance",
                  "eventsOutsideClass": "attendance", "disciplineEvents": "attendance"}
HOMEWORK_NOTICE = re.compile(r'שיעורי[\s-]*בית\s*"(.+?)"\s*בשיעור\s+(.+?)\s+בתאריך\s+(\d{1,2}/\d{1,2}/\d{4})', re.S)
MULTI_USERS_ENDPOINT = "user/GetMultipleUsersForUser"
CHANGE_USER_ENDPOINT = "user/ChangeUser"


class ViewBlocked(Exception):
    """The school turned this pupil-card module off for students/parents."""
WEEKS = (-4, -3, -2, -1, 0, 1)

DATE_KEYS = ("lessonDate", "date", "Date", "dayDate", "fullDate", "lesson_date")
SUBJECT_KEYS = ("subject_name", "subjectName", "professionName", "subject", "lessonName", "courseName")
HW_KEYS = ("homeWork", "homework", "homeworkText", "hw")
DUE_KEYS = ("dueDate", "homeworkDueDate", "homeWorkDate", "toDate", "submissionDate")
_SKIP_HEADERS = {"content-length", "cookie", "host", "accept-encoding"}


def _clean(text: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


class Smartschool(Source):
    key = "smartschool"
    label = "סמארטסקול"

    def is_logged_in(self, page: Page) -> bool:
        url = page.url.lower()
        return "webtop.smartschool.co.il" in url and "/account/" not in url and "loginmoe" not in url

    def start_login(self, page: Page):
        page.goto(f"{SITE}/dashboard", wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        if not self.is_logged_in(page):
            page.goto(MOE_ENTRY, wait_until="domcontentloaded")

    def direct_login(self, page: Page, username: str, password: str, interactive: bool) -> str:
        """Webtop's own username/password form (accounts without MoE SSO).

        The form has a visible reCAPTCHA; the 'כניסה' button stays disabled until it's ticked, so
        the first login is normally completed by the parent in the visible window. We tick
        'זכור אותי' so the session survives in this child's browser profile afterwards.
        """
        page.goto(f"{SITE}/dashboard", wait_until="domcontentloaded")
        _wait_for(page, lambda: self.is_logged_in(page) or "/account/login" in page.url, 15)
        page.wait_for_timeout(1500)
        if self.is_logged_in(page):
            return LoginResult.OK
        if "/account/login" not in page.url:
            page.goto(f"{SITE}/account/login", wait_until="domcontentloaded")

        user_box = page.locator("form input[type=text]").first
        user_box.wait_for(state="visible", timeout=20000)
        page.wait_for_timeout(2500)   # captcha + 'remember me' render a moment after the inputs
        # The login button stays disabled until the cookie banner is accepted (the parent approved
        # accepting it on their behalf). Saved in this account's browser profile afterwards.
        cookies_btn = page.locator("#allowCookiesBtn")
        if cookies_btn.count() and cookies_btn.is_visible():
            cookies_btn.click()
        if username and password:
            type_into(user_box, username)
            type_into(page.locator("form input[type=password]").first, password)
        remember = page.locator("input[type=checkbox][id^=mat-mdc-checkbox]").first
        if remember.count() and not remember.is_checked():
            page.get_by_text("זכור אותי").first.click()

        login_btn = page.get_by_role("button", name="כניסה", exact=True)
        if username and password and login_btn.is_enabled():
            login_btn.click()
            if _wait_for(page, lambda: self.is_logged_in(page), 30):
                return LoginResult.OK
            if not interactive and "שגוי" in page.inner_text("body"):
                return LoginResult.BAD_CREDENTIALS
        if interactive and _wait_for(page, lambda: self.is_logged_in(page), 300):
            return LoginResult.OK
        return LoginResult.NEEDS_USER

    def fetch(self, page: Page, raw_dir: Path) -> list[dict]:
        self.identity = None
        items, blocked, post = self._fetch_school(page, raw_dir, "")

        # A parent linked to several schools has one Webtop user per school (the switcher in the
        # site header). Switch to each one, collect its children, then switch back.
        try:
            users = post(MULTI_USERS_ENDPOINT, {}) or []
        except Exception:
            users = []
        users = [u for u in users if isinstance(u, dict) and u.get("studentId") and u.get("isTeacher") != 1]
        dump(raw_dir, "linked_users", [{k: u.get(k) for k in ("school_name", "institutionCode", "userType")} for u in users])
        if len(users) > 1:
            original = page.evaluate("() => localStorage.getItem('selectedUser')")
            others = [u for u in users if u["studentId"] != original]
            try:
                for n, u in enumerate(others, 1):
                    self._switch_user(post, page, u)
                    more, more_blocked, _ = self._fetch_school(page, raw_dir, f"school{n}_")
                    items.update(more)
                    blocked += more_blocked
            finally:
                back = next((u for u in users if u["studentId"] == original), None)
                if back:
                    try:
                        self._switch_user(post, page, back)
                    except Exception:
                        pass
        if blocked and not items:
            raise RuntimeError("בית הספר חסם את הצפייה ב: " + ", ".join(sorted(set(blocked))))
        return list(items.values())

    def _switch_user(self, post, page: Page, user: dict):
        saved = page.evaluate("() => localStorage.getItem('SavedUser')")
        post(CHANGE_USER_ENDPOINT, {"StudentId": user["studentId"], "institutionCode": user.get("institutionCode"),
                                    "userType": user.get("userType"), "savedUser": saved, "deviceId": None})
        page.evaluate("id => { localStorage.setItem('selectedUser', id); sessionStorage.setItem('selectedUser', id); }",
                      user["studentId"])

    def _fetch_school(self, page: Page, raw_dir: Path, prefix: str):
        """Everything for the Webtop user currently active: returns (items, blocked, post)."""
        captured, settings = [], []

        def on_response(r):
            if r.request.method != "POST":
                return
            if HW_ENDPOINT in r.url:
                captured.append(r)
            elif SETTINGS_ENDPOINT in r.url:
                try:
                    settings.append(r.json().get("data") or {})
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            page.goto(HOMEWORK_PAGE, wait_until="domcontentloaded")
            for _ in range(40):
                if captured:
                    break
                page.wait_for_timeout(500)
        finally:
            page.remove_listener("response", on_response)
        if not captured:
            raise RuntimeError("דף שיעורי הבית לא נטען (ייתכן שהמודול חסום בבית הספר)")

        req = captured[0].request
        body = req.post_data_json or {}
        headers = {k: v for k, v in req.headers.items() if k.lower() not in _SKIP_HEADERS and not k.startswith(":")}
        dump(raw_dir, f"{prefix}request", {"url": req.url, "body": body})

        # A parent account gets its children from GetSettingsList; the page only loads the first
        # one, so we replay the request for each child (same as picking them in the dropdown).
        students = [None]
        if not prefix and body.get("studentName"):
            self.identity = [body["studentName"]]   # the pupil the card was opened for
        if settings and settings[0].get("isParent") and settings[0].get("children"):
            self.identity = None   # a parent account: identity is checked per child instead
            students = settings[0]["children"]
            dump(raw_dir, f"{prefix}children", [{k: s.get(k) for k in ("id", "firstName", "lastName", "classCode")} for s in students])

        api = req.url.split("/api/")[0] + "/api/"

        def post(endpoint, payload):
            data = page.request.post(api + endpoint, data=json.dumps(payload), headers=headers).json()
            if not data.get("status", True) and data.get("errorDescription") == "view is blocked":
                raise ViewBlocked()
            return data.get("data") if data.get("status", True) else None

        items, blocked = {}, []
        for i, st in enumerate(students):
            tag = f"{prefix}s{i}" if st else f"{prefix}me"
            base = dict(body)
            if st:
                base.update(studentID=st["id"], classCode=st.get("classCode", base.get("classCode")),
                            studentName=f"{st.get('lastName', '')} {st.get('firstName', '')}".strip())
            found = []
            # homework, per week
            for week in WEEKS:
                try:
                    data = post(HW_ENDPOINT, {**base, "weekIndex": week})
                except ViewBlocked:
                    blocked.append("שיעורי בית")
                    break
                dump(raw_dir, f"homework_{tag}_{week}", data)
                found += self._parse(data or [])
            # grades (pupil card module 6) and notifications; one failing doesn't stop the others
            for name, endpoint, payload, parser in (
                ("ציונים", GRADES_ENDPOINT, {**base, "moduleID": 6}, self._parse_grades),
                ("התראות", NOTIFICATIONS_ENDPOINT, {"id": base.get("studentID")}, self._parse_notifications),
            ):
                try:
                    data = post(endpoint, payload)
                    dump(raw_dir, f"{parser.__name__[7:]}_{tag}", data)
                    found += parser(data or [])
                except ViewBlocked:
                    blocked.append(name)
                except Exception as e:
                    dump(raw_dir, f"{parser.__name__[7:]}_{tag}_error", {"error": str(e)})
            for t in found:
                if st:
                    t["for_student"] = {"first": (st.get("firstName") or "").strip(),
                                        "last": (st.get("lastName") or "").strip()}
                items[(tag, t.get("kind", "task"), t["external_id"])] = t
        return items, blocked, post

    def _parse_grades(self, data) -> list[dict]:
        out = []
        for g in data if isinstance(data, list) else []:
            if not isinstance(g, dict):
                continue
            score = first(g, "grade", "gradeTranslation", "score", "finalGrade")
            if score in (None, ""):
                continue
            subject = first(g, "subject", "subjectName", "professionName")
            title = first(g, "title", "evaluationEventName", "eventName", "gradeTypeName", "typeName", "description", "name")
            date = to_iso_date(first(g, "date", "eventDate", "gradeDate"))
            translation = g.get("gradeTranslation")
            out.append({
                "kind": "grade",
                "external_id": str(first(g, "id", "evaluationID", "gradeID", "eventID") or stable_id(date, subject, title)),
                "title": str(title or subject or "ציון"),
                "subject": subject,
                "score": score if not translation or translation == score else f"{score} ({translation})",
                "assigned_date": date,
                "description": first(g, "remark", "remarks", "note", "comment", "teacherRemark"),
                "url": f"{SITE}/Student_Card/6",
                "raw": g,
            })
        return out

    def _parse_notifications(self, data) -> list[dict]:
        if isinstance(data, dict):
            data = data.get("notifications") or []
        out = []
        for n in data if isinstance(data, list) else []:
            if not isinstance(n, dict):
                continue
            text = _clean(str(first(n, "message", "text", "title", "body") or ""))
            if not text:
                continue
            date = to_iso_date(first(n, "date", "createDate", "sendDate"))
            nav = n.get("moduleNavigation")
            out.append({
                "kind": "notice",
                # Smartschool sends "id": 0 on every notification, so it can't be the key
                "external_id": stable_id(date, text),
                "title": text.splitlines()[0][:200],
                "description": text if "\n" in text or len(text) > 200 else None,
                "category": NOTICE_MODULES.get(n.get("moduleName")) or notice_category(text),
                "assigned_date": date,
                "source_status": "נקרא" if n.get("read_date") else "לא נקרא",
                "url": f"{SITE}/{nav}" if nav else f"{SITE}/notifications",
                "raw": n,
            })
            hw = HOMEWORK_NOTICE.search(text)
            if n.get("moduleName") == "lessonSubjectHomework" and hw:
                # "עודכנו עבורך שיעורי-בית "<text>" בשיעור <subject> בתאריך dd/mm/yyyy": the homework itself
                body, subject, lesson_date = hw.group(1).strip(), hw.group(2).strip(), to_iso_date(hw.group(3))
                out.append({
                    "external_id": stable_id(lesson_date, subject, body),   # same key as the pupil-card homework
                    "title": body.splitlines()[0][:140],
                    "subject": subject,
                    "description": body if len(body) > 140 else None,
                    "assigned_date": lesson_date or date,
                    "url": HOMEWORK_PAGE,
                    "raw": n,
                })
        return out

    def _parse(self, data) -> list[dict]:
        out = []

        def walk(node, date=None, subject=None):
            if isinstance(node, list):
                for x in node:
                    walk(x, date, subject)
                return
            if not isinstance(node, dict):
                return
            date = to_iso_date(first(node, *DATE_KEYS)) or date
            subj = first(node, *SUBJECT_KEYS)
            subject = subj if isinstance(subj, str) else subject
            hw = first(node, *HW_KEYS)
            if isinstance(hw, str) and _clean(hw):
                text = _clean(hw)
                out.append({
                    "external_id": stable_id(date, subject, text),
                    "title": text.splitlines()[0][:140],
                    "subject": subject,
                    "description": text if "\n" in text or len(text) > 140 else None,
                    "assigned_date": date,
                    "due_date": to_iso_date(first(node, *DUE_KEYS)),
                    "url": HOMEWORK_PAGE,
                    "raw": node,
                })
            for v in node.values():
                if isinstance(v, (list, dict)):
                    walk(v, date, subject)

        walk(data)
        return out
