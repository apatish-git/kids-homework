"""Runs syncs one at a time on a dedicated worker thread (Playwright's sync API is thread-bound).

Each child gets its own persistent browser profile (data/profiles/child-<id>), so cookies from
one child never mix with another's, and a successful login is reused on later syncs.
"""
import json
import queue
import threading
import traceback

from playwright.sync_api import sync_playwright

from . import credentials, db, moe_login
from .moe_login import LoginResult, complete_login
from .sources import SOURCES
from .sources.ofek import SchoolNotChosen

PROFILES = db.DATA_DIR / "profiles"
RAW = db.DATA_DIR / "raw"

_jobs: "queue.Queue[tuple[int, bool]]" = queue.Queue()
_pending: set[int] = set()
status = {"running": None, "step": ""}   # read by the UI


def enqueue(child_id: int, interactive: bool = False):
    if child_id in _pending:
        return
    _pending.add(child_id)
    _jobs.put((child_id, interactive))
    child = db.get_child(child_id)
    parent = _smartschool_parent(child) if child else None
    if parent and not interactive:
        enqueue(parent["id"])   # this child's Smartschool data comes from the parent account
    elif child and child["login_method"] == "none":
        for p in db.list_children():   # not linked yet: any parent account may bring this child
            if p["login_method"] == "webtop":
                enqueue(p["id"])


def enqueue_all():
    for ch in db.list_children():
        enqueue(ch["id"])


def queued() -> list[int]:
    return sorted(_pending)


def _worker():
    while True:
        child_id, interactive = _jobs.get()
        try:
            sync_child(child_id, interactive)
        except Exception:
            traceback.print_exc()
        finally:
            _pending.discard(child_id)
            status.update(running=None, step="")


def start_worker():
    threading.Thread(target=_worker, daemon=True, name="sync-worker").start()


def _match_child(student: dict, account_id: int) -> int:
    """Map a child listed under a parent account to a child in the tool, by first name."""
    first = student["first"]
    full = f"{first} {student['last']}".strip()
    for ch in db.list_children():
        if ch["id"] == account_id or not first:
            continue
        names = [ch["name"], *(ch.get("alt_names") or "").split(",")]
        for name in (n.strip() for n in names if n.strip()):
            if name == first or name == full or name.split()[0] == first:
                return ch["id"]
    return account_id


def _smartschool_parent(child: dict):
    """The parent Webtop account covering this child's Smartschool data, if it still exists."""
    via = child.get("smartschool_via")
    parent = db.get_child(via) if via else None
    return parent if parent and parent["login_method"] == "webtop" else None


def _name_words(child: dict) -> set[str]:
    """Words of the names the parent typed for this child (name + 'other names')."""
    typed = [child["name"], *(child.get("alt_names") or "").split(",")]
    return {w for n in typed for w in n.split() if len(w) > 1}


def _same_person(child: dict, names: list[str]) -> bool:
    """Is a name the site shows this child's?
    - a full name the site showed earlier for this child matches as a whole, in any word order;
    - otherwise a word of the parent-typed names must appear, ignoring words siblings' names
      share too, so the family name alone never matches a sibling."""
    seen = {frozenset(n.split()) for n in json.loads(child.get("seen_names") or "[]")}
    if any(frozenset(n.split()) in seen for n in names):
        return True
    shared = set().union(*[_name_words(c) for c in db.list_children() if c["id"] != child["id"]])
    mine = _name_words(child) - shared
    return any(w in mine for n in names for w in n.split())


def _log_out(ctx, page):
    """Forget every login in this child's browser profile (MoE single sign-on and the sites)."""
    try:
        page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
    except Exception:
        pass
    ctx.clear_cookies()


def _safe_screenshot(page, path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path))
    except Exception:
        pass


CHILD_COLORS = ["#4f7cff", "#e8590c", "#2b8a3e", "#ae3ec9", "#e03131", "#0c8599"]


def _create_linked_child(student: dict, parent_id: int) -> int:
    """A child listed under the parent account who isn't in the tool yet (e.g. no login of her
    own): add her as a no-login user fed by this parent account, and move anything an older
    version filed under the parent ('<name> · <subject>') over to her."""
    used = {c["color"] for c in db.list_children()}
    color = next((c for c in CHILD_COLORS if c not in used), CHILD_COLORS[0])
    new_id = db.add_child(student["first"], "", color, "", "none")
    full = f"{student['first']} {student['last']}".strip()
    db.update_child(new_id, smartschool_via=parent_id, alt_names=full if full != student["first"] else "")
    db.move_prefixed_items(parent_id, new_id, student["first"])
    return new_id


def _child_name(child_id: int) -> str:
    ch = db.get_child(child_id)
    return ch["name"] if ch else "?"


def sync_child(child_id: int, interactive: bool = False):
    child = db.get_child(child_id)
    if not child or child["login_method"] == "none":
        return   # a no-login child is filled in by the parent account's sync (queued by enqueue)
    password = credentials.get_password(child_id)
    if child["login_state"] == "bad_credentials" and not interactive:
        password = None   # don't keep retrying a wrong password and get the account locked
    status.update(running=child["name"], step="פותח דפדפן")
    states = []

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILES / f"child-{child_id}"),
            headless=not interactive,
            locale="he-IL",
            # visible window: fit the real screen (a fixed viewport hides the page bottom on scaled displays)
            **({"no_viewport": True, "args": ["--start-maximized"]} if interactive
               else {"viewport": {"width": 1280, "height": 900}}),
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            direct = child["login_method"] == "webtop"
            keys = ["smartschool"] if direct else [s for s in child["sources"].split(",") if s in SOURCES]
            if not direct and _smartschool_parent(child):
                keys = [k for k in keys if k != "smartschool"]   # the parent account already brings it
            for key in keys:
                src = SOURCES[key]
                run_id = db.start_run(child_id, key)
                try:
                    status["step"] = f"{src.label}: התחברות"
                    if direct:
                        result = src.direct_login(page, child["moe_username"], password, interactive)
                    else:
                        if hasattr(src, "configure"):
                            src.configure(child, interactive)
                        src.start_login(page)
                        try:
                            result = complete_login(page, child["moe_username"], password, src.is_logged_in, interactive)
                        except SchoolNotChosen:
                            db.update_child(child_id, ofek_school_options=json.dumps(src.school_options, ensure_ascii=False))
                            states.append("needs_user")
                            db.finish_run(run_id, "needs_user", "אופק מבקש לבחור בית ספר. יש לבחור אותו ב'עריכה' של הילד")
                            continue
                    if result != LoginResult.OK:
                        states.append(result)
                        if result == LoginResult.BAD_CREDENTIALS:
                            password = None   # never retry a rejected password on the next source
                            msg = "האתר דחה את הכניסה: " + (moe_login.last_error or "שם משתמש או סיסמה שגויים")
                            _safe_screenshot(page, RAW / f"child-{child_id}" / "login_error.png")
                        else:
                            msg = ("נדרשת השלמת התחברות ידנית (קוד SMS / אימות)" if password
                                   else "נדרשת התחברות ידנית")
                            host = page.url.split("/")[2] if "//" in page.url else page.url
                            msg += f" · נעצר ב־{host}"
                            _safe_screenshot(page, RAW / f"child-{child_id}" / f"login_{key}.png")
                        db.finish_run(run_id, result, msg)
                        continue
                    status["step"] = f"{src.label}: מושך משימות"
                    tasks = src.fetch(page, RAW / f"child-{child_id}" / key)
                    who = src.identity or []
                    # MoE login is single sign-on: a reused session could belong to someone else
                    # (e.g. after a manual login with other details). If the site shows another
                    # name, log out and log in again with this child's own credentials.
                    if not direct and who and not _same_person(child, who) and not moe_login.typed_credentials:
                        status["step"] = f"{src.label}: מחובר כמשתמש אחר, מתחבר מחדש"
                        _log_out(ctx, page)
                        src.start_login(page)
                        result = complete_login(page, child["moe_username"], password, src.is_logged_in, interactive)
                        if result != LoginResult.OK:
                            states.append(result)
                            db.finish_run(run_id, result, f"היה מחובר בתור {who[0]}. ההתחברות מחדש לא הושלמה")
                            continue
                        tasks = src.fetch(page, RAW / f"child-{child_id}" / key)
                        who = src.identity or []
                    if not direct and who and not _same_person(child, who):
                        if not moe_login.typed_credentials:
                            states.append("error")
                            db.finish_run(run_id, "error", f"{src.label}: האתר מחובר בתור {who[0]} ולא בתור {child['name']}")
                            continue
                        # We just typed this child's own details, so these names are theirs.
                        seen = json.loads(child.get("seen_names") or "[]")
                        child["seen_names"] = json.dumps(sorted(set(seen) | set(who)), ensure_ascii=False)
                        db.update_child(child_id, seen_names=child["seen_names"])
                    routed = {}
                    for t in tasks:
                        target = child_id
                        st = t.pop("for_student", None)
                        if st:   # parent account: file it under the matching child
                            target = _match_child(st, child_id)
                            if target == child_id and st["first"]:
                                target = _create_linked_child(st, child_id)
                            routed.setdefault(st["first"] or "?", _child_name(target))
                            if target != child_id:   # this parent account now covers that child's Smartschool
                                db.update_child(target, smartschool_via=child_id)
                        db.upsert_task(target, key, t)
                    counts = {k: sum(1 for t in tasks if t.get("kind", "task") == k) for k in ("task", "grade", "notice")}
                    msg = f"משימות: {counts['task']} · ציונים: {counts['grade']} · התראות: {counts['notice']}"
                    if who:
                        msg = f"מחובר בתור {who[0]} · " + msg
                    if routed:
                        msg += " · " + ", ".join(f"{k} ← {v}" for k, v in routed.items())
                    states.append("ok")
                    db.finish_run(run_id, "ok", msg, len(tasks))
                except Exception as e:
                    states.append("error")
                    traceback.print_exc()   # full details stay in the server window
                    db.finish_run(run_id, "error", f"{src.label}: {str(e).splitlines()[0][:200]}")
        finally:
            ctx.close()

    login_state = next((s for s in ("bad_credentials", "needs_user", "error") if s in states), "ok")
    errors = [r["message"] for r in db.recent_runs(5) if r["child_id"] == child_id and r["status"] != "ok"]
    db.update_child(child_id, login_state=login_state, last_sync=db.now(),
                    last_error=errors[0] if errors and login_state != "ok" else None)
