"""Remote view for "manual login" on a machine with no screen (e.g. a Linux server).

The login browser runs headless; the /remote page shows it as a live image and forwards the
parent's clicks and typing. Playwright may only be used from the sync thread, so the web
handlers just queue events and read the latest frame; tick() runs inside the sync thread's
wait loop (moe_login.tick_hook), applies the queued events and takes a new screenshot.
"""
import os
import queue
import sys
import threading
import time

_lock = threading.Lock()
_events: "queue.Queue[tuple]" = queue.Queue()
state = {"active": False, "child": None, "frame": None, "size": (0, 0), "updated": 0.0, "url": ""}


def has_display() -> bool:
    """Can a visible browser window be shown on this machine?"""
    if not sys.platform.startswith("linux"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def start(child_name: str):
    with _lock:
        while not _events.empty():
            _events.get_nowait()
        state.update(active=True, child=child_name, frame=None, updated=0.0, url="")


def stop():
    with _lock:
        state.update(active=False, child=None, frame=None, url="")


def push(event: tuple):
    if state["active"]:
        _events.put(event)


def frame():
    with _lock:
        return state["frame"], state["updated"]


def tick(page):
    """Sync thread only: apply queued input, then refresh the screenshot."""
    if not state["active"]:
        return
    try:
        while not _events.empty():
            kind, *args = _events.get_nowait()
            if kind == "click":
                page.mouse.click(*args)
            elif kind == "type":
                page.keyboard.type(args[0], delay=30)
            elif kind == "key":
                page.keyboard.press(args[0])
            elif kind == "scroll":
                page.mouse.wheel(0, args[0])
        shot = page.screenshot(type="jpeg", quality=70)
        size = page.viewport_size or {"width": 1280, "height": 900}
        with _lock:
            state.update(frame=shot, size=(size["width"], size["height"]), updated=time.time(), url=page.url)
    except Exception:
        pass   # the page may be mid-navigation; the next tick will catch up
