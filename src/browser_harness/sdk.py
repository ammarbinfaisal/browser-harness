"""Transport-independent browser-harness SDK for an already leased browser.

The embedding runner supplies the transport. Importing this module never attaches
to a daemon, launches Chrome, or discovers another agent's tabs.
"""
_transport = None
_recorder = None
_sequence = 0
_parents = []


def bind(transport, recorder=None):
    global _transport, _recorder
    _transport = transport
    _recorder = recorder


def _call(name, **args):
    if _transport is None:
        raise RuntimeError("browser-harness SDK has no bound browser transport")
    return _transport(name, args)


def goto_url(url):
    return _call("page.goto", url=url)


def page_info():
    return _call("harness.page_info")


def current_tab():
    return _call("harness.current_tab")


def list_tabs():
    return [{**tab, "targetId": tab["id"], "target_id": tab["id"]} for tab in _call("tabs.list")]


def switch_tab(target, activate=False):
    if isinstance(target, dict):
        target = target.get("targetId") or target.get("target_id") or target.get("id")
    return _call("tabs.switch", id=target)


def activate_tab(target):
    return switch_tab(target, activate=True)


def new_tab(url="about:blank"):
    tab = _call("harness.new_tab")
    switch_tab(tab)
    if url != "about:blank":
        goto_url(url)
    return current_tab()


def js(expression, argument=None, **kwargs):
    return _call("harness.js", expression=expression, argument=argument, **kwargs)


def browser_request(url, *, method="GET", body=None, headers=None, semantics="write"):
    """Same-origin, browser-authenticated request; POST may have read semantics."""
    return _call("harness.request", url=url, method=method, body=body,
                 headers=headers or {}, semantics=semantics)


def observe(selector="body", **kwargs):
    return _call("harness.observe", selector=selector, **kwargs)


def find(selector, **kwargs):
    return _call("harness.find", selector=selector, **kwargs)


def click(selector, **kwargs):
    return _call("harness.click", selector=selector, **kwargs)


def fill_input(selector, text, **kwargs):
    return _call("harness.fill", selector=selector, text=text, **kwargs)


def click_at_xy(x, y, **kwargs):
    return _call("harness.click_at_xy", x=x, y=y, **kwargs)


def type_text(text):
    return _call("harness.type_text", text=text)


def press_key(key, modifiers=0):
    return _call("harness.press_key", key=key, modifiers=modifiers)


def clipboard_write(text):
    return _call("harness.clipboard_write", text=text)


def paste(text=None):
    return _call("harness.paste", **({"text": text} if text is not None else {}))


def accessibility_tree(selector="body"):
    return _call("harness.accessibility_tree", selector=selector)


def scroll(x=0, y=0, dy=300, dx=0):
    return _call("harness.scroll", x=x, y=y, dy=dy, dx=dx)


def wait_for_element(selector, **kwargs):
    return _call("harness.wait_for_element", selector=selector, **kwargs)


def wait_for_load():
    return _call("page.waitForLoadState", state="load")


def capture_screenshot():
    return _call("harness.screenshot")


def screenshot():
    return capture_screenshot()


def upload_file(selector, path):
    import base64
    from pathlib import Path
    return _call("harness.upload", selector=selector, name=Path(path).name,
                 base64=base64.b64encode(Path(path).read_bytes()).decode("ascii"))


def download_file(selector, path=None):
    import base64
    from pathlib import Path
    result = _call("harness.download", selector=selector)
    destination = Path(path or Path(result["name"]).name)
    destination.write_bytes(base64.b64decode(result["base64"], validate=True))
    return str(destination)


def frames():
    return _call("harness.frames")


def select_all():
    return press_key("ControlOrMeta+A")


__all__ = [name for name, value in list(globals().items())
           if not name.startswith("_") and name != "bind" and callable(value)]


def _traced(fn):
    import functools
    import time
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        global _sequence
        _sequence += 1
        identity = _sequence
        event = {"sdkCallId": identity, "name": fn.__name__, "parent": _parents[-1] if _parents else None}
        started = time.monotonic()
        if _recorder:
            _recorder({**event, "phase": "started", "args": args, "kwargs": kwargs})
        _parents.append(identity)
        try:
            result = fn(*args, **kwargs)
            if _recorder:
                _recorder({**event, "phase": "finished", "result": result, "durationMs": (time.monotonic()-started)*1000})
            return result
        except BaseException as error:
            if _recorder:
                _recorder({**event, "phase": "finished", "error": str(error), "durationMs": (time.monotonic()-started)*1000})
            raise
        finally:
            _parents.pop()
    return wrapped


for _name in __all__:
    globals()[_name] = _traced(globals()[_name])
