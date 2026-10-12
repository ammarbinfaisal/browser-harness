"""The kernel side of scoped recovery: a compiled module's `with browser.scope(...)` block fails,
the host runs agent cells inside the paused module through the suspended call loop, and the
module resumes with the value the host answers with."""
import json

import pytest

from browser_harness.tabductor_runner import Interpreter

PAGE = {"$ref": {"id": "p", "class": "Page", "scope": "s"}}
CONTEXT = {"$ref": {"id": "c", "class": "BrowserContext", "scope": "s"}}
LOCATOR = {"$ref": {"id": "l", "class": "Locator", "scope": "s"}}


class FakeHost:
    """Both ends of the wire: answers the kernel's calls and, during a scope recovery, feeds the
    scripted agent cells one at a time before answering the recovery itself."""

    def __init__(self, recovery_cells=(), recovery_reply=None, click_fails=True):
        self.inbound, self.frames, self.calls = [], [], []
        self.recovery_cells, self.recovery_reply = list(recovery_cells), recovery_reply
        self.recover_id, self.click_fails, self.buffer = None, click_fails, ""

    # wire_in
    def readline(self):
        return self.inbound.pop(0) if self.inbound else ""

    # wire_out
    def write(self, text):
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self.handle(json.loads(line))

    def flush(self):
        pass

    def send(self, frame):
        self.inbound.append(json.dumps(frame) + "\n")

    def reply(self, identity, result):
        self.send({"id": identity, "result": result})

    def handle(self, frame):
        self.frames.append(frame)
        kind = frame.get("type")
        if kind == "cell_result":
            if self.recovery_cells:
                self.send({"type": "cell", "id": f"cell-{len(self.recovery_cells)}", "source": self.recovery_cells.pop(0), "scope": self.scope_key})
            else:
                self.reply(self.recover_id, self.recovery_reply)
            return
        if kind != "call":
            return
        self.calls.append(frame)
        name, args, identity = frame["name"], frame["args"], frame["id"]
        if name == "playwright.open":
            self.reply(identity, {"ok": True, "value": {"page": PAGE, "context": CONTEXT}})
        elif name == "playwright.call":
            member = args["member"]
            if member == "locator":
                self.reply(identity, {"ok": True, "value": LOCATOR})
            elif member == "count":
                self.reply(identity, {"ok": True, "value": 1})
            elif member == "click" and self.click_fails:
                self.reply(identity, {"ok": False, "error": "Target closed", "code": "browser_error"})
            else:
                self.reply(identity, {"ok": True, "value": None})
        elif name == "workflow.scope.recover":
            self.recover_id, self.scope_key = identity, args["scopeKey"]
            if self.recovery_cells:
                self.send({"type": "cell", "id": f"cell-{len(self.recovery_cells)}", "source": self.recovery_cells.pop(0), "scope": self.scope_key})
            else:
                self.reply(identity, self.recovery_reply)
        else:  # workflow.done / workflow.resume / playwright.close / anything else
            self.reply(identity, {"ok": True, "value": None})


MODULE = """
def run(page, context, browser):
    rows = [1, 2]
    with browser.scope("open-card", goal="Open the first card", returns={"type": "object"}) as s:
        cards = page.locator('.card')
        cards.click()
        s.value = {"count": 0}
    browser.done(result={"resumed": s.resumed, "value": s.value, "rows": rows})
"""


def execute(host, source, compiled=True):
    interp = Interpreter(host, host)
    return interp.execute({"source": source, "compiled": compiled, "input": {}, "repl": False})


def test_scope_failure_runs_agent_cells_in_the_paused_module_and_resumes():
    host = FakeHost(recovery_cells=["print(cards, len(rows)); rows.append(page.locator('li').count())",
                                    "browser.resume(value={'count': len(rows)})"],
                    recovery_reply={"ok": True, "value": {"value": {"count": 3}}})
    result = execute(host, MODULE)
    assert result["ok"] is True
    recover = next(c for c in host.calls if c["name"] == "workflow.scope.recover")
    assert recover["args"]["scopeId"] == "open-card" and recover["args"]["cause"] == "error"
    assert "Target closed" in recover["args"]["reason"] and recover["scope"] is None and recover["cell"] is None
    assert {v["name"] for v in recover["args"]["locals"]} == {"cards", "rows", "s"} - {"s"}
    # The failing click was made under the scope; the recovery cells' calls name their cell.
    click = next(c for c in host.calls if c["name"] == "playwright.call" and c["args"]["member"] == "click")
    assert click["scope"] == "open-card"
    nested_calls = [c for c in host.calls if c.get("cell")]
    assert [c["args"].get("member", c["name"]) for c in nested_calls] == ["locator", "count", "workflow.resume"]
    assert {c["cell"] for c in nested_calls} == {"cell-2", "cell-1"}
    outputs = [f for f in host.frames if f.get("type") == "output"]
    assert "".join(f["text"] for f in outputs) == "<Locator remote> 2\n" and {f["cell"] for f in outputs} == {"cell-2"}
    results = [f for f in host.frames if f.get("type") == "cell_result"]
    assert results[0]["ok"] is True and results[0]["namespace"]["inherited"] == ["cards", "rows"]
    assert results[1]["ok"] is True and results[1].get("terminal") is True
    done = next(c for c in host.calls if c["name"] == "workflow.done")
    # The script continued with the host's value; the list the agent appended to is the script's own.
    assert done["args"]["result"] == {"resumed": True, "value": {"count": 3}, "rows": [1, 2, 1]}


def test_escalated_recovery_ends_the_module_without_completing():
    host = FakeHost(recovery_reply={"ok": True, "terminal": True})
    result = execute(host, MODULE)
    assert result["ok"] is True
    assert not any(c["name"] == "workflow.done" for c in host.calls)


def test_scope_deopt_is_a_guard_and_nested_cells_cannot_open_scopes():
    host = FakeHost(recovery_cells=["with browser.scope('inner', goal='x'): pass", "browser.resume(value=7)"],
                    recovery_reply={"ok": True, "value": {"value": 7}})
    result = execute(host, """
def run(page, context, browser):
    with browser.scope("pick", goal="Pick", returns={"type": "integer"}) as s:
        n = page.locator('li').count()
        if n < 5:
            s.deopt("no option matched", {"seen": n})
        s.value = n
    browser.done(result=s.value)
""")
    assert result["ok"] is True
    recover = next(c for c in host.calls if c["name"] == "workflow.scope.recover")
    assert recover["args"]["cause"] == "guard" and recover["args"]["reason"] == "no option matched"
    assert recover["args"]["evidence"] == {"seen": 1} and recover["args"]["error"] is None
    results = [f for f in host.frames if f.get("type") == "cell_result"]
    assert results[0]["ok"] is False and "recovery cell" in results[0]["error"]
    assert next(c for c in host.calls if c["name"] == "workflow.done")["args"]["result"] == 7


def test_scope_is_refused_outside_compiled_modules():
    host = FakeHost()
    result = execute(host, "with browser.scope('s', goal='g'): pass", compiled=False)
    assert result["ok"] is False and "compiled modules" in result["error"]


@pytest.mark.parametrize("kwargs", [dict(id="Bad Id", goal="g"), dict(id="ok", goal=""), dict(id="ok", goal="g", returns=3)])
def test_scope_arguments_are_validated(kwargs):
    host = FakeHost()
    result = execute(host, f"def run(page, context, browser):\n    browser.scope(**{kwargs!r})")
    assert result["ok"] is False and "browser.scope" in result["error"]


def test_a_cell_frame_outside_a_recovery_is_refused():
    host = FakeHost()
    interp = Interpreter(host, host)
    interp.run_nested_cell({"id": "stray", "source": "print(1)", "scope": "none"})
    assert host.frames[-1] == {"type": "cell_result", "id": "stray", "ok": False, "error": "No open scope recovery accepts cells",
                               "namespace": {"defined": [], "inherited": []}}
