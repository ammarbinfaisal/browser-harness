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
import base64
import subprocess
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import browser_harness
from browser_harness import sdk


class Terminal(BaseException):
    pass


def main():
    wire_in, wire_out = sys.stdin, sys.stdout
    wire_out.write(json.dumps({"type": "ready", "protocolVersion": 2, "sdkVersion": "browser-harness-python-v2"}) + "\n")
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
        wire_out.write(json.dumps({"type": "call", "id": sequence, "name": name, "args": args or {}, "parent": parents[-1] if parents else None,
                                   "sdkCallId": sdk._parents[-1] if sdk._parents else None}) + "\n")
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
    # Imports and pre-injected globals share the same run-scoped gateway.
    browser_harness.api = browser_harness.td = api
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

    def sdk_record(event):
        wire_out.write(json.dumps({"type": "sdk", "event": event}) + "\n")
        wire_out.flush()
    sdk.bind(call, sdk_record)
    env.update({name: getattr(sdk, name) for name in sdk.__all__})
    files_before = config.get("workspace")
    if files_before is not None:
        for existing in workspace.rglob("*"):
            if existing.is_symlink() or existing.is_file():
                existing.unlink()
        for name in files_before:
            item = call("workspace.read", {"path": name})
            destination = workspace / name
            if not destination.resolve().is_relative_to(workspace.resolve()):
                raise ValueError("Invalid workspace path")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(base64.b64decode(item["content"], validate=True)
                                    if item["encoding"] == "base64" else item["content"].encode())

    def checkpoint_files():
        if files_before is None:
            return
        files, total = {}, 0
        for item in sorted(workspace.rglob("*")):
            if item.is_symlink():
                raise ValueError("Workspace snapshots cannot contain symlinks")
            if not item.is_file() or "__pycache__" in item.parts:
                continue
            if item.stat().st_size > 8_000_000:
                raise ValueError("Workspace file exceeds 8 MB")
            data = item.read_bytes()
            total += len(data)
            if total > 32_000_000 or len(files) >= 256:
                raise ValueError("Workspace budget exceeded")
            try:
                value = {"content": data.decode("utf8"), "encoding": "utf8"}
            except UnicodeDecodeError:
                value = {"content": base64.b64encode(data).decode(), "encoding": "base64"}
            files[item.relative_to(workspace).as_posix()] = value
        call("workspace.commit", {"files": files})

    api.checkpoint_files = checkpoint_files
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
    result = {"type": "result", "ok": True}
    try:
        with contextlib.redirect_stdout(sys.stderr):
            exec(compile(config["source"], "browser.py", "exec"), env)
    except Terminal:
        pass
    except BaseException:
        result = {"type": "result", "ok": False, "error": traceback.format_exc(limit=8)[-4000:]}
    try:
        checkpoint_files()
        changed = (workspace / "agent_helpers.py").read_text()
        if changed != (library["source"] if library else ""):
            call("helpers.define", {"name": "agent_helpers", "source": changed or "# empty helper module\n"})
    except BaseException:
        result = {"type": "result", "ok": False, "error": traceback.format_exc(limit=8)[-4000:]}
    wire_out.write(json.dumps(result) + "\n")
    wire_out.flush()


def supervise():
    """One sandbox per run; each submitted cell has a fresh interpreter."""
    for config in sys.stdin:
        child = subprocess.Popen([sys.executable, "-I", __file__], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def stderr():
            for line in child.stderr:
                sys.stderr.write(line)
                sys.stderr.flush()
        output = threading.Thread(target=stderr)
        output.start()
        child.stdin.write(config)
        child.stdin.flush()
        result = None
        for line in child.stdout:
            message = json.loads(line)
            if message.get("type") == "result":
                result = line
                continue
            sys.stdout.write(line)
            sys.stdout.flush()
            if message.get("type") == "call":
                response = sys.stdin.readline()
                if not response:
                    child.kill()
                    break
                child.stdin.write(response)
                child.stdin.flush()
        child.wait()
        output.join()
        sys.stdout.write(result or json.dumps({"type": "result", "ok": False, "error": "Python process terminated; inspect the effect journal"}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    supervise() if "--supervise" in sys.argv else main()
