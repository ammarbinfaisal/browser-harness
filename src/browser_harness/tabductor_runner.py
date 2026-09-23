"""Isolated Python runner. All browser effects cross the recorded stdio gateway.

Run in the supplied networkless container, never in the trusted worker process.
stdout is protocol-only; user output goes to stderr. No browser credentials enter
this process. The host, not these messages, decides completion and write receipts.
"""
import contextlib
import ast
import functools
import json
import sys
import traceback
from pathlib import Path


class Terminal(BaseException):
    pass


def main():
    wire_in, wire_out = sys.stdin, sys.stdout
    wire_out.write(json.dumps({"type": "ready"}) + "\n")
    wire_out.flush()
    config = json.loads(wire_in.readline())
    sequence, parents = 0, []
    loading_helper = False

    def call(name, args=None):
        nonlocal sequence
        if loading_helper:
            raise RuntimeError("Browser/workflow operations belong inside helper functions, not module initialization")
        if name == "helpers.define":
            helper_name, source = args["name"], args["source"]
            tree = ast.parse(source)
            if not helper_name.isidentifier() or helper_name in ("api", "td"):
                raise ValueError("helper name must be a public Python identifier other than api/td")
            if helper_name != "agent_helpers" and not any(isinstance(node, ast.FunctionDef) and node.name == helper_name for node in tree.body):
                raise ValueError(f"Helper must define {helper_name}")
        sequence += 1
        wire_out.write(json.dumps({"type": "call", "id": sequence, "name": name, "args": args or {}, "parent": parents[-1] if parents else None}) + "\n")
        wire_out.flush()
        response = json.loads(wire_in.readline())
        if response.get("id") != sequence:
            raise RuntimeError("gateway response does not match operation")
        result = response["result"]
        if name in ("done", "fail", "run.deopt") and result.get("ok"):
            raise Terminal()
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "Operation failed"))
        return result.get("value")

    class Namespace:
        def __init__(self, path=""):
            self.path = path

        def __getattr__(self, name):
            if name.startswith("_"):
                raise AttributeError(name)
            return Namespace(f"{self.path}.{name}" if self.path else name)

        def __call__(self, args=None, **kwargs):
            name = {"run.done": "done", "run.fail": "fail"}.get(self.path, self.path)
            return call(name, args if args is not None else kwargs)

    api = Namespace()
    api.input = config.get("input")
    workspace = Path.cwd()
    env = {"__name__": "__agent__", "api": api, "td": api}

    def wrap(fn, item):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            call("helpers.use", {"name": item["name"], "revision": item["revision"]})
            parents.append({"name": item["name"], "revision": item["revision"]})
            try:
                return fn(*args, **kwargs)
            finally:
                parents.pop()
        return wrapped

    def install(item):
        nonlocal loading_helper
        scope = dict(env)
        call("helpers.use", {"name": item["name"], "revision": item["revision"]})
        loading_helper = True
        try:
            with contextlib.redirect_stdout(sys.stderr):
                exec(compile(item["source"], f"{item['name']}.py", "exec"), scope)
        finally:
            loading_helper = False
        if item["name"] == "agent_helpers":
            env.update({k: wrap(v, item) for k, v in scope.items()
                        if not k.startswith("_") and getattr(v, "__globals__", None) is scope and k not in env})
            return scope
        fn = scope.get(item["name"])
        if not callable(fn):
            raise ValueError(f"Helper must define {item['name']}")
        env[item["name"]] = wrap(fn, item)
        return scope

    # Familiar upstream helper names; browser implementation is supplied by Camoufox.
    env.update({
        "goto_url": lambda url: api.page.goto(url=url),
        "page_info": lambda: api.harness.observe(),
        "observe": lambda **kwargs: api.harness.observe(**kwargs),
        "find": lambda selector, **kwargs: api.harness.find(selector=selector, **kwargs),
        "click": lambda selector, **kwargs: api.harness.click(selector=selector, **kwargs),
        "click_at_xy": lambda x, y, **kwargs: api.harness.click_at_xy(x=x, y=y, **kwargs),
        "fill_input": lambda selector, text, **kwargs: api.harness.fill(selector=selector, text=text, **kwargs),
        "type_text": lambda text: api.harness.type_text(text=text),
        "press_key": lambda key: api.harness.press_key(key=key),
        "scroll": lambda x=0, y=0, dy=300, dx=0: api.harness.scroll(x=x, y=y, dy=dy, dx=dx),
        "js": lambda expression, argument=None, **kwargs: api.harness.js(expression=expression, argument=argument, **kwargs),
        "capture_screenshot": lambda: api.harness.screenshot(),
        "list_tabs": lambda: api.tabs.list(),
        "switch_tab": lambda target: api.tabs.switch(id=target),
        "wait_for_load": lambda: api.page.waitForLoadState(state="load"),
        "wait_for_element": lambda selector, **kwargs: api.harness.wait_for_element(selector=selector, **kwargs),
        "upload_file": lambda selector, path: api.harness.upload(selector=selector, name=Path(path).name,
            base64=__import__("base64").b64encode(Path(path).read_bytes()).decode("ascii")),
    })
    scopes = []
    for item in config.get("helpers", []):
        try:
            scopes.append(install(item))
        except Exception as error:
            # A broken module must not prevent a later cell from repairing it.
            print(f"Could not load helper {item['name']}: {error}", file=sys.stderr)
    for scope in scopes:
        scope.update({name: value for name, value in env.items() if not name.startswith("_")})
    # Persist edits to agent_helpers.py as a single task-scoped module revision.
    library = next((h for h in config.get("helpers", []) if h["name"] == "agent_helpers"), None)
    (workspace / "agent_helpers.py").write_text(library["source"] if library else "")
    try:
        with contextlib.redirect_stdout(sys.stderr):
            exec(compile(config["source"], "browser.py", "exec"), env)
    except Terminal:
        wire_out.write(json.dumps({"type": "result", "ok": True}) + "\n")
        wire_out.flush()
        return
    except BaseException:
        wire_out.write(json.dumps({"type": "result", "ok": False, "error": traceback.format_exc(limit=8)[-4000:]}) + "\n")
        wire_out.flush()
        return
    changed = (workspace / "agent_helpers.py").read_text()
    if changed and changed != (library["source"] if library else ""):
        call("helpers.define", {"name": "agent_helpers", "source": changed})
    wire_out.write(json.dumps({"type": "result", "ok": True}) + "\n")
    wire_out.flush()


if __name__ == "__main__":
    main()
