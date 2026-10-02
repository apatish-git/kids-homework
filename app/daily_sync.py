"""Daily sync, started by Windows Task Scheduler (see winsched.py).

If the web app is running, ask it to sync (so two browsers never open the same profile);
otherwise run the sync right here, in the background. Output goes to data/daily_sync.log.
"""
import json
import logging
import time
import urllib.request

from . import db, sync

APP = "http://127.0.0.1:8765"
log = logging.getLogger("daily_sync")


def _app_running() -> bool:
    try:
        with urllib.request.urlopen(f"{APP}/api/status", timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


def main():
    logging.basicConfig(filename=db.DATA_DIR / "daily_sync.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(message)s")
    db.init()
    if _app_running():
        urllib.request.urlopen(urllib.request.Request(f"{APP}/sync-all", data=b"", method="POST"), timeout=10)
        log.info("the app is open: asked it to sync all")
        return
    log.info("the app is closed: syncing here")
    start = time.time()
    for child in db.list_children():
        if child["login_method"] == "none":
            continue   # filled in by the parent account's sync
        try:
            sync.sync_child(child["id"])
        except Exception:
            log.exception("sync failed for child %s", child["id"])
    states = {c["name"]: c["login_state"] for c in db.list_children() if c["login_method"] != "none"}
    log.info("done in %ds: %s", time.time() - start, json.dumps(states, ensure_ascii=False))


if __name__ == "__main__":
    main()
