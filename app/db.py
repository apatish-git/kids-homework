"""SQLite storage: children, tasks, sync runs."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "homework.db"

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS children (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    moe_username TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT '#4f7cff',
    sources TEXT NOT NULL DEFAULT 'smartschool,ofek',
    login_state TEXT NOT NULL DEFAULT 'unknown',   -- unknown | ok | needs_user | error
    last_sync TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id INTEGER NOT NULL REFERENCES children(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    subject TEXT,
    description TEXT,
    due_date TEXT,
    assigned_date TEXT,
    url TEXT,
    source_status TEXT,
    done INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    raw TEXT,
    UNIQUE(child_id, source, external_id)
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id INTEGER,
    source TEXT,
    started TEXT,
    finished TEXT,
    status TEXT,
    message TEXT,
    found INTEGER DEFAULT 0
);
"""


@contextmanager
def conn():
    with _lock:
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            yield c
            c.commit()
        finally:
            c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(children)")}
        if "login_method" not in cols:   # 'moe' = MoE SSO, 'webtop' = Smartschool username/password
            c.execute("ALTER TABLE children ADD COLUMN login_method TEXT NOT NULL DEFAULT 'moe'")
        if "alt_names" not in cols:   # other names the sites use for this child (e.g. a full name vs. a nickname)
            c.execute("ALTER TABLE children ADD COLUMN alt_names TEXT NOT NULL DEFAULT ''")
        if "ofek_school" not in cols:   # which school to pick in CET's "בחירת בית ספר" dialog
            c.execute("ALTER TABLE children ADD COLUMN ofek_school TEXT NOT NULL DEFAULT ''")
            c.execute("ALTER TABLE children ADD COLUMN ofek_school_options TEXT NOT NULL DEFAULT '[]'")
        if "seen_names" not in cols:   # names the sites showed right after logging in with this child's details
            c.execute("ALTER TABLE children ADD COLUMN seen_names TEXT NOT NULL DEFAULT '[]'")
        if "smartschool_via" not in cols:   # parent Webtop account that already brings this child's Smartschool data
            c.execute("ALTER TABLE children ADD COLUMN smartschool_via INTEGER")
        tcols = {r["name"] for r in c.execute("PRAGMA table_info(tasks)")}
        if "kind" not in tcols:   # the tasks table also holds grades and site notifications
            c.execute("ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'task'")
            c.execute("ALTER TABLE tasks ADD COLUMN score TEXT")
            c.execute("ALTER TABLE tasks ADD COLUMN category TEXT")
        if "seen_at" not in tcols:   # when a list page first showed this item; NULL = "new" tag
            c.execute("ALTER TABLE tasks ADD COLUMN seen_at TEXT")
            c.execute("UPDATE tasks SET seen_at=?", (now(),))   # only items arriving from now on count as new


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- children ----------

def list_children():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM children ORDER BY id")]


def get_child(child_id: int):
    with conn() as c:
        r = c.execute("SELECT * FROM children WHERE id=?", (child_id,)).fetchone()
        return dict(r) if r else None


def add_child(name, moe_username, color, sources, login_method="moe") -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO children(name, moe_username, color, sources, login_method) VALUES (?,?,?,?,?)",
            (name, moe_username, color, sources, login_method),
        )
        return cur.lastrowid


def update_child(child_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE children SET {cols} WHERE id=?", (*fields.values(), child_id))


def delete_child(child_id):
    with conn() as c:
        c.execute("DELETE FROM children WHERE id=?", (child_id,))


# ---------- tasks ----------

def upsert_task(child_id, source, t: dict):
    """Store one item. kind: 'task' (homework/assignment), 'grade', or 'notice' (site notification)."""
    ts = now()
    kind = t.get("kind", "task")
    ext_id = t["external_id"] if kind == "task" else f"{kind}:{t['external_id']}"
    with conn() as c:
        c.execute(
            """
            INSERT INTO tasks(child_id, source, external_id, kind, title, subject, description, due_date,
                              assigned_date, url, source_status, score, category, first_seen, last_seen, raw)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(child_id, source, external_id) DO UPDATE SET
                title=excluded.title, subject=excluded.subject, description=excluded.description,
                due_date=excluded.due_date, assigned_date=excluded.assigned_date, url=excluded.url,
                source_status=excluded.source_status, score=excluded.score, category=excluded.category,
                last_seen=excluded.last_seen, raw=excluded.raw
            """,
            (
                child_id, source, ext_id, kind, t["title"], t.get("subject"),
                t.get("description"), t.get("due_date"), t.get("assigned_date"), t.get("url"),
                t.get("source_status"), None if t.get("score") is None else str(t["score"]), t.get("category"),
                ts, ts, json.dumps(t.get("raw"), ensure_ascii=False)[:20000],
            ),
        )


def list_tasks(child_id=None, include_done=False, kind="task"):
    q = """SELECT t.*, ch.name AS child_name, ch.color AS child_color
           FROM tasks t JOIN children ch ON ch.id = t.child_id WHERE t.kind=?"""
    args = [kind]
    if child_id:
        q += " AND t.child_id=?"
        args.append(child_id)
    if not include_done:
        q += " AND t.done=0"
    if kind == "task":
        q += " ORDER BY COALESCE(t.due_date, t.assigned_date) IS NULL, COALESCE(t.due_date, t.assigned_date), t.child_id"
    else:   # grades & notices: newest first
        q += " ORDER BY COALESCE(t.assigned_date, t.first_seen) DESC, t.id DESC"
    with conn() as c:
        return [dict(r) for r in c.execute(q, args)]


def move_prefixed_items(from_child, to_child, name):
    """Items stored under a parent with subject '<name> · <subject>' (or just '<name>') move to the child."""
    prefix = f"{name} · "
    with conn() as c:
        rows = c.execute("SELECT id, subject FROM tasks WHERE child_id=? AND (subject=? OR subject LIKE ?)",
                         (from_child, name, prefix + "%")).fetchall()
        for r in rows:
            subject = r["subject"][len(prefix):] if r["subject"].startswith(prefix) else None
            try:
                c.execute("UPDATE tasks SET child_id=?, subject=? WHERE id=?", (to_child, subject, r["id"]))
            except sqlite3.IntegrityError:   # already there under the child
                c.execute("DELETE FROM tasks WHERE id=?", (r["id"],))
    return len(rows)


def set_task_done(task_id, done: bool):
    set_done_many([task_id], done)


def set_done_many(ids, done: bool):
    with conn() as c:
        c.executemany("UPDATE tasks SET done=? WHERE id=?", [(1 if done else 0, int(i)) for i in ids])


def unseen_counts() -> dict:
    """How many items of each kind haven't been shown on their page yet (for the top-bar badges)."""
    with conn() as c:
        rows = c.execute("SELECT kind, COUNT(*) AS n FROM tasks WHERE seen_at IS NULL AND done=0 GROUP BY kind")
        return {r["kind"]: r["n"] for r in rows}


def mark_seen(items):
    """Flag items as shown, so the 'new' tag appears only the first time. Returns the items with is_new set."""
    new_ids = [t["id"] for t in items if not t.get("seen_at")]
    for t in items:
        t["is_new"] = not t.get("seen_at")
    if new_ids:
        with conn() as c:
            c.executemany("UPDATE tasks SET seen_at=? WHERE id=?", [(now(), i) for i in new_ids])
    return items


# ---------- sync runs ----------

def start_run(child_id, source) -> int:
    with conn() as c:
        return c.execute(
            "INSERT INTO sync_runs(child_id, source, started, status) VALUES (?,?,?,'running')",
            (child_id, source, now()),
        ).lastrowid


def finish_run(run_id, status, message="", found=0):
    with conn() as c:
        c.execute(
            "UPDATE sync_runs SET finished=?, status=?, message=?, found=? WHERE id=?",
            (now(), status, message[:2000], found, run_id),
        )


def recent_runs(limit=30):
    with conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT r.*, ch.name AS child_name FROM sync_runs r
               LEFT JOIN children ch ON ch.id=r.child_id ORDER BY r.id DESC LIMIT ?""", (limit,))]
