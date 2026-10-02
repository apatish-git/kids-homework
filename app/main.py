import json
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import credentials, db, sync, winsched
from .sources import SOURCES
from .sources.base import CATEGORY_LABELS

BASE = Path(__file__).resolve().parent
SETTINGS_FILE = db.DATA_DIR / "settings.json"
DEFAULT_SETTINGS = {"sync_every_hours": 0, "daily_enabled": False, "daily_time": "16:00"}
COLORS = ["#4f7cff", "#e8590c", "#2b8a3e", "#ae3ec9", "#e03131", "#0c8599"]

app = FastAPI(title="בית ספר חכם")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.globals["unseen_counts"] = db.unseen_counts   # top-bar "new" badges, read at render time
scheduler = BackgroundScheduler()


def load_settings():
    try:
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_FILE.read_text(encoding="utf8"))}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)


def schedule(hours: int):
    scheduler.remove_all_jobs()
    if hours > 0:
        scheduler.add_job(sync.enqueue_all, "interval", hours=hours, id="auto-sync")


@app.on_event("startup")
def startup():
    db.init()
    sync.start_worker()
    scheduler.start()
    schedule(load_settings()["sync_every_hours"])


OLD_HOMEWORK_DAYS = 21
ACTIVE_BUCKETS = ("overdue", "today", "tomorrow", "week", "later", "given", "nodate")


def submitted(t) -> bool:
    return (t["source_status"] or "").startswith("הוגש")


def bucket_tasks(tasks):
    """The one rule for where a task shows — the tasks page and the summary both use it.
    'old' = homework with no due date given more than 3 weeks ago (shown collapsed, not counted)."""
    today = date.today()
    buckets = {k: [] for k in (*ACTIVE_BUCKETS, "old")}
    for t in tasks:
        due = date.fromisoformat(t["due_date"]) if t["due_date"] else None
        given = date.fromisoformat(t["assigned_date"]) if t["assigned_date"] else None
        if due:
            delta = (due - today).days
            key = ("overdue" if delta < 0 else "today" if delta == 0 else "tomorrow" if delta == 1
                   else "week" if delta <= 7 else "later")
            if key == "overdue" and submitted(t):
                continue   # handed in: nothing left to do
            buckets[key].append(t)
        elif given:
            buckets["given" if (today - given).days <= OLD_HOMEWORK_DAYS else "old"].append(t)
        else:
            buckets["nodate"].append(t)
    buckets["given"].sort(key=lambda t: t["assigned_date"], reverse=True)
    buckets["old"].sort(key=lambda t: t["assigned_date"], reverse=True)
    return buckets


def open_count(buckets, keys=ACTIVE_BUCKETS) -> int:
    """Tasks still to do: shown in an active section and not handed in."""
    return sum(1 for k in keys for t in buckets[k] if not submitted(t))


def _num(score):
    try:
        return float(str(score).split()[0])
    except (TypeError, ValueError, IndexError):
        return None


@app.get("/")
def summary(request: Request):
    today = date.today()
    days = [today + timedelta(days=i) for i in range(14)]
    month_ago, week_ago = (today - timedelta(days=30)).isoformat(), (today - timedelta(days=7)).isoformat()
    all_children = db.list_children()
    tasks = db.list_tasks()
    grades = db.list_tasks(include_done=True, kind="grade")
    notices = db.list_tasks(include_done=True, kind="notice")

    kids = []
    for ch in all_children:
        my_tasks = [t for t in tasks if t["child_id"] == ch["id"]]
        b = bucket_tasks(my_tasks)   # same rule as the tasks page
        mine = [t for k in ACTIVE_BUCKETS for t in b[k] if not submitted(t)]
        my_grades = [g for g in grades if g["child_id"] == ch["id"]]
        my_notices = [n for n in notices if n["child_id"] == ch["id"]]
        if ch["login_method"] == "webtop" and not (my_tasks or my_grades or my_notices):
            continue   # a parent account whose data all went to the children
        due = [t for t in mine if t["due_date"]]
        recent_nums = [_num(g["score"]) for g in my_grades if (g["assigned_date"] or "") >= month_ago]
        recent_nums = [n for n in recent_nums if n is not None]
        kids.append({
            **ch,
            "open": open_count(b),
            "overdue": open_count(b, ("overdue",)),
            "week": open_count(b, ("today", "tomorrow", "week")),
            "old": len(b["old"]),
            # not yet shown on a list page in this app (the "new" tag), not dismissed
            "new_notices": sum(1 for t in [*my_tasks, *my_grades, *my_notices] if not t["seen_at"] and not t["done"]),
            "next_due": min((t for t in due if t["due_date"] >= today.isoformat()), key=lambda t: t["due_date"], default=None),
            "last_grade": my_grades[0] if my_grades else None,
            "avg": round(sum(recent_nums) / len(recent_nums)) if recent_nums else None,
            "load": [sum(1 for t in due if t["due_date"] == d.isoformat()) for d in days],
            "by_category": {k: sum(1 for n in my_notices if n["category"] == k and (n["assigned_date"] or "") >= month_ago)
                            for k in CATEGORY_LABELS},
        })

    recent_grades = [g for g in grades if _num(g["score"]) is not None][:10]
    activity = sorted([*tasks, *grades, *notices], key=lambda t: (t["first_seen"], t["assigned_date"] or ""), reverse=True)[:12]
    last_sync = max((c["last_sync"] for c in all_children if c["last_sync"]), default=None)
    return templates.TemplateResponse(request, "summary.html", {
        "kids": kids, "days": days, "today": today, "recent_grades": recent_grades, "activity": activity,
        "categories": CATEGORY_LABELS, "sources": SOURCES, "last_sync": last_sync,
        "totals": {k: sum(c[k] for c in kids) for k in ("open", "overdue", "week", "new_notices")},
        "max_load": max([1, *[n for c in kids for n in c["load"]]]),
        "max_cat": max([1, *[sum(c["by_category"].values()) for c in kids]]),
        "weekday": lambda d: "אבגדהוש"[(d.weekday() + 1) % 7],
        "num": _num,
    })


@app.get("/tasks")
def index(request: Request, child: int | None = None):
    children = db.list_children()
    tasks = db.list_tasks(child)
    since = (date.today() - timedelta(days=14)).isoformat()
    hw_notices = [n for n in db.list_tasks(child, kind="notice")
                  if n["category"] == "homework" and (n["assigned_date"] or n["first_seen"][:10]) >= since]
    buckets = bucket_tasks(tasks)
    db.mark_seen([t for k in ACTIVE_BUCKETS for t in buckets[k]] + hw_notices)   # "new" tag shows this once
    db.mark_seen(buckets["old"])   # listed (collapsed) on this page too, so not "new" in the top bar anymore
    for t in buckets["old"]:
        t["is_new"] = False
    return templates.TemplateResponse(request, "index.html", {
        "children": children, "selected": child, "buckets": buckets, "hw_notices": hw_notices,
        "open_counts": {k: open_count(buckets, (k,)) for k in buckets},
        "sources": SOURCES, "today": date.today().isoformat(),
        "tomorrow": (date.today() + timedelta(days=1)).isoformat(),
    })


@app.get("/grades")
def grades_page(request: Request, child: int | None = None):
    return templates.TemplateResponse(request, "grades.html", {
        "children": db.list_children(), "selected": child, "sources": SOURCES,
        "grades": db.mark_seen(db.list_tasks(child, include_done=True, kind="grade")),
    })


@app.get("/notices")
def notices_page(request: Request, child: int | None = None, category: str = "", show_all: int = 0):
    notices = db.list_tasks(child, include_done=bool(show_all), kind="notice")
    if category:
        notices = [n for n in notices if n["category"] == category]
    db.mark_seen(notices)
    return templates.TemplateResponse(request, "notices.html", {
        "children": db.list_children(), "selected": child, "sources": SOURCES, "notices": notices,
        "category": category, "show_all": show_all, "categories": CATEGORY_LABELS,
    })


@app.get("/children")
def children_page(request: Request, msg: str = ""):
    children = db.list_children()
    for ch in children:
        ch["has_password"] = credentials.get_password(ch["id"]) is not None
        ch["school_options"] = json.loads(ch.get("ofek_school_options") or "[]")
        via = next((p for p in children if p["id"] == ch.get("smartschool_via") and p["login_method"] == "webtop"), None)
        ch["ss_via_name"] = via["name"] if via else None
    return templates.TemplateResponse(request, "children.html", {
        "children": children, "sources": SOURCES, "settings": load_settings(),
        "next_color": COLORS[len(children) % len(COLORS)],
        "task": winsched.status(), "msg": msg,
    })


def _method_and_sources(login_method: str, sources: list[str], default=""):
    """'moe' = MoE SSO, 'webtop' = Webtop username/password, 'none' = no login (data via a parent account)."""
    method = login_method if login_method in ("moe", "webtop", "none") else "moe"
    srcs = {"webtop": "smartschool", "none": ""}.get(method)
    return method, srcs if srcs is not None else (",".join(s for s in sources if s in SOURCES) or default)


@app.post("/children")
def add_child(name: str = Form(...), moe_username: str = Form(""), password: str = Form(""),
              color: str = Form("#4f7cff"), sources: list[str] = Form([]), login_method: str = Form("moe"),
              alt_names: str = Form("")):
    login_method, srcs = _method_and_sources(login_method, sources, "smartschool,ofek")
    child_id = db.add_child(name.strip(), moe_username.strip(), color, srcs, login_method)
    db.update_child(child_id, alt_names=",".join(n.strip() for n in alt_names.split(",") if n.strip()))
    if password and login_method != "none":
        credentials.set_password(child_id, password)
    return RedirectResponse("/children", status_code=303)


@app.post("/children/{child_id}/update")
def update_child(child_id: int, name: str = Form(...), moe_username: str = Form(""), color: str = Form(...),
                 password: str = Form(""), sources: list[str] = Form([]), login_method: str = Form("moe"),
                 alt_names: str = Form(""), ofek_school: str = Form("")):
    login_method, srcs = _method_and_sources(login_method, sources)
    db.update_child(child_id, name=name.strip(), moe_username=moe_username.strip(), color=color,
                    sources=srcs, login_method=login_method,
                    alt_names=",".join(n.strip() for n in alt_names.split(",") if n.strip()),
                    ofek_school=ofek_school.strip())
    if password:
        credentials.set_password(child_id, password)
        db.update_child(child_id, login_state="unknown", last_error=None)
    return RedirectResponse("/children", status_code=303)


@app.post("/children/{child_id}/delete")
def delete_child(child_id: int):
    db.delete_child(child_id)
    credentials.delete_password(child_id)
    return RedirectResponse("/children", status_code=303)


@app.post("/children/{child_id}/sync")
def sync_child(child_id: int, interactive: int = 0, back: str = "/"):
    if not db.get_child(child_id):
        raise HTTPException(404)
    sync.enqueue(child_id, bool(interactive))
    return RedirectResponse(back if back.startswith("/") else "/", status_code=303)


@app.post("/sync-all")
def sync_all(back: str = "/"):
    sync.enqueue_all()
    return RedirectResponse(back if back.startswith("/") else "/", status_code=303)


@app.post("/settings")
def save_settings(sync_every_hours: int = Form(0), daily_enabled: str = Form(""), daily_time: str = Form("16:00")):
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", daily_time):
        daily_time = "16:00"
    s = {**load_settings(), "sync_every_hours": max(0, min(sync_every_hours, 48)),
         "daily_enabled": bool(daily_enabled), "daily_time": daily_time}
    SETTINGS_FILE.write_text(json.dumps(s), encoding="utf8")
    schedule(s["sync_every_hours"])
    try:
        winsched.register(daily_time) if s["daily_enabled"] else winsched.unregister()
        msg = ""
    except Exception as e:
        msg = f"לא הצלחתי לעדכן את מתזמן המשימות של Windows: {str(e).splitlines()[0][:200]}"
    return RedirectResponse("/children" + (f"?msg={quote(msg)}" if msg else "#schedule"), status_code=303)


@app.post("/tasks/done-many")
async def tasks_done_many(request: Request):
    body = await request.json()
    db.set_done_many(body.get("ids") or [], bool(body.get("done")))
    return {"ok": True}


@app.post("/tasks/{task_id}/done")
async def task_done(task_id: int, request: Request):
    body = await request.json()
    db.set_task_done(task_id, bool(body.get("done")))
    return {"ok": True}


@app.get("/api/status")
def api_status():
    return JSONResponse({**sync.status, "queued": sync.queued()})


@app.get("/log")
def log_page(request: Request):
    raw = []
    for f in sorted([*sync.RAW.glob("child-*/*.png"), *sync.RAW.glob("child-*/*/*.json")]):
        raw.append(f.relative_to(sync.RAW).as_posix())
    return templates.TemplateResponse(request, "log.html", {"runs": db.recent_runs(60), "raw": raw})


@app.get("/raw/{path:path}")
def raw_file(path: str):
    f = (sync.RAW / path).resolve()
    if not f.is_file() or sync.RAW.resolve() not in f.parents:
        raise HTTPException(404)
    return FileResponse(f)
