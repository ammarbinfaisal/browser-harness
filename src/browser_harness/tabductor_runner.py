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
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import browser_harness
from browser_harness.playwright_proxy import (
    BrowserError, BrowserTimeoutError, PUBLIC_EXPORTS, Transport, Workflow, install_sync_api,
)


class Terminal(BaseException):
    pass


class WorkflowError(Exception):
    """A rejected workflow API call, not a bug in the submitted Python program."""


def validate_compiled_source(source):
    """A conservative compilation lint; the container remains the security boundary."""
    tree = ast.parse(source)
    forbidden_modules = {"socket", "subprocess", "ctypes", "multiprocessing", "threading", "importlib", "sys", "builtins", "playwright", "camoufox"}
    forbidden_calls = {"exec", "eval", "compile", "__import__", "getattr", "setattr", "delattr", "globals", "locals", "vars"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                if (name.name.split(".")[0] in forbidden_modules and name.name not in {"playwright", "playwright.sync_api"}) or name.name.startswith("browser_harness."):
                    raise ValueError("Compiled program imports a runtime or ambient-execution module")
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if (module.split(".")[0] in forbidden_modules and module not in {"playwright", "playwright.sync_api"}) or module.startswith("browser_harness."):
                raise ValueError("Compiled program imports a runtime or ambient-execution module")
            if module == "playwright" and any(n.name != "sync_api" for n in node.names):
                raise ValueError("Only playwright.sync_api can be imported")
            if module == "playwright.sync_api" and any(n.name not in PUBLIC_EXPORTS for n in node.names):
                raise ValueError("Only public browser objects can be imported")
            if module == "browser_harness" and any(n.name not in {"expect", "page", "context", "workflow"} for n in node.names):
                raise ValueError("Only public browser objects can be imported")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("Compiled programs cannot access runtime internals")
        if isinstance(node, ast.Name) and node.id in forbidden_calls:
            raise ValueError("Dynamic Python execution and introspection cannot be compiled")
    return tree


def main():
    wire_in, wire_out = sys.stdin, sys.stdout
    wire_out.write(json.dumps({"type": "ready", "protocolVersion": 3, "sdkVersion": "playwright-python-v1"}) + "\n")
    wire_out.flush()
    config = json.loads(wire_in.readline())
    # Syntax errors must not allocate browser objects or mutate workspace files.
    try:
        program = compile(config["source"], "browser.py", "exec")
    except SyntaxError as error:
        message = f"Python syntax error on line {error.lineno}: {error.msg}. Nothing was executed."
        if "await" in error.msg or "async" in error.msg:
            message += " The browser API is synchronous: use print(page.title()) or page.locator('body').inner_text() without await or async def."
        wire_out.write(json.dumps({"type": "result", "ok": False, "error": message}) + "\n")
        wire_out.flush()
        return
    if config.get("compiled"):
        validate_compiled_source(config["source"])
        for helper in config.get("helpers", []):
            validate_compiled_source(helper["source"])
    sequence, parents = 0, []
    callback_stack, pending = [], {}
    loading_helper = False

    def call(name, args=None):
        nonlocal sequence
        if loading_helper and not name.startswith("internal."):
            raise RuntimeError("Browser/workflow operations belong inside helper functions, not module initialization")
        if name == "internal.helpers.define":
            helper_name, source = args["name"], args["source"]
            tree = ast.parse(source)
            if not helper_name.isidentifier() or helper_name in ("api", "td"):
                raise ValueError("helper name must be a public Python identifier other than api/td")
            if helper_name != "agent_helpers" and not any(isinstance(node, ast.FunctionDef) and node.name == helper_name for node in tree.body):
                raise ValueError(f"Helper must define {helper_name}")
        sequence += 1
        wire_out.write(json.dumps({"type": "call", "id": sequence, "name": name, "args": args or {}, "parent": parents[-1] if parents else None,
                                   "callbackId": callback_stack[-1] if callback_stack else None}) + "\n")
        wire_out.flush()
        identity = sequence
        while identity not in pending:
            line = wire_in.readline()
            if not line:
                raise RuntimeError("Host disconnected; reconcile pending effects")
            response = json.loads(line)
            if response.get("type") == "callback":
                event = response["event"]
                callback_stack.append(event["id"])
                try:
                    with contextlib.redirect_stdout(sys.stderr):
                        value = transport.callback(event)
                    wire_out.write(json.dumps({"type":"callback_result","id":event["id"],"ok":True,"value":value})+"\n")
                except BaseException as error:
                    wire_out.write(json.dumps({"type":"callback_result","id":event["id"],"ok":False,"error":str(error)[:1000]})+"\n")
                    if isinstance(error, Terminal):
                        raise
                finally:
                    callback_stack.pop()
                    wire_out.flush()
            else:
                pending[response["id"]] = response["result"]
        result = pending.pop(identity)
        if name in ("workflow.done", "workflow.fail", "workflow.deopt", "workflow.yield_control") and result.get("ok"):
            raise Terminal()
        if not result.get("ok"):
            if result.get("code") == "browser_assertion_failed":
                raise AssertionError(result.get("error", "Playwright assertion failed"))
            if name.startswith("playwright."):
                error_type = BrowserTimeoutError if result.get("code") == "browser_timeout" else BrowserError
            else:
                error_type = WorkflowError if name.startswith("workflow.") else RuntimeError
            raise error_type(result.get("error", "Operation failed"))
        return result.get("value")

    transport = Transport(call)
    workflow = Workflow(transport, input=config.get("input"))
    roots = transport.decode(call("playwright.open"))
    page, context = roots["page"], roots["context"]
    install_sync_api(transport, page, context)
    # Retain old imports for recorded programs and persisted helpers.
    browser_harness.page = page
    browser_harness.context = context
    browser_harness.expect = transport.expect
    browser_harness.workflow = workflow
    workspace = Path.cwd()
    env = {"__name__": "__agent__", "page": page, "context": context, "expect": transport.expect, "workflow": workflow}

    def wrap(fn, item):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            call("internal.helpers.use", {"name": item["name"], "revision": item["revision"]})
            parents.append({"name": item["name"], "revision": item["revision"]})
            try:
                return fn(*args, **kwargs)
            finally:
                parents.pop()
        return wrapped

    def install(item):
        nonlocal loading_helper
        module = types.ModuleType(item["name"])
        scope = module.__dict__
        scope.update(env)
        sys.modules[item["name"]] = module
        call("internal.helpers.use", {"name": item["name"], "revision": item["revision"]})
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

    files_before = config.get("workspace")
    if files_before is not None:
        for existing in workspace.rglob("*"):
            if existing.is_symlink() or existing.is_file():
                existing.unlink()
        for name in files_before:
            item = call("internal.workspace.read", {"path": name})
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
        call("internal.workspace.commit", {"files": files})

    sys.path.insert(0, str(workspace))
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
            exec(program, env)
            if config.get("compiled"):
                env["run"](page, context, workflow)
    except Terminal:
        pass
    except BaseException as error:
        result = {"type": "result", "ok": False,
                  "error": str(error) if isinstance(error, (BrowserError, WorkflowError)) else traceback.format_exc(limit=8)[-4000:]}
    try:
        checkpoint_files()
        changed = (workspace / "agent_helpers.py").read_text()
        if not config.get("compiled") and changed != (library["source"] if library else ""):
            call("internal.helpers.define", {"name": "agent_helpers", "source": changed or "# empty helper module\n"})
    except BaseException:
        result = {"type": "result", "ok": False, "error": traceback.format_exc(limit=8)[-4000:]}
    try:
        call("playwright.close")
    except BaseException:
        pass
    wire_out.write(json.dumps(result) + "\n")
    wire_out.flush()


def supervise():
    """Forward both directions independently so callbacks may issue nested calls."""
    child = None
    lock = threading.Lock()

    def forward(process):
        nonlocal child
        def stderr():
            for line in process.stderr:
                sys.stderr.write(line)
                sys.stderr.flush()
        output = threading.Thread(target=stderr)
        output.start()
        result = None
        for line in process.stdout:
            if json.loads(line).get("type") == "result":
                result = line
            else:
                sys.stdout.write(line)
                sys.stdout.flush()
        process.wait()
        output.join()
        with lock:
            child = None
            sys.stdout.write(result or json.dumps({"type":"result","ok":False,"error":"Python process terminated; inspect the current page before retrying"}) + "\n")
            sys.stdout.flush()

    for line in sys.stdin:
        with lock:
            if child is None:
                child = subprocess.Popen([sys.executable, "-I", __file__], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                threading.Thread(target=forward, args=(child,), daemon=True).start()
            child.stdin.write(line)
            child.stdin.flush()
    if child:
        child.kill()


if __name__ == "__main__":
    supervise() if "--supervise" in sys.argv else main()
