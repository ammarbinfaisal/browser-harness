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
import types
import uuid
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
            if module == "browser_harness" and any(n.name not in {"expect", "page", "context", "workflow", "browser"} for n in node.names):
                raise ValueError("Only public browser objects can be imported")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("Compiled programs cannot access runtime internals")
        if isinstance(node, ast.Name) and node.id in forbidden_calls:
            raise ValueError("Dynamic Python execution and introspection cannot be compiled")
    return tree



class CellOutput:
    """Output shares the protocol stream so a result cannot overtake printed text."""
    def __init__(self, wire):
        self.wire = wire

    def write(self, text):
        if text:
            self.wire.write(json.dumps({"type": "output", "text": text}) + "\n")
            self.wire.flush()
        return len(text)

    def flush(self):
        self.wire.flush()


class Interpreter:
    """One namespace, transport and browser-object scope for a run-owned REPL."""
    def __init__(self, wire_in, wire_out):
        self.wire_in, self.wire_out = wire_in, wire_out
        self.session_id = str(uuid.uuid4())
        self.output = CellOutput(wire_out)
        self.sequence, self.parents = 0, []
        self.callback_stack, self.pending = [], {}
        self.loading_helper = False
        self.transport = Transport(self.call)
        self.env = {"__name__": "__agent__"}
        self.helper_revisions, self.helper_scopes = {}, {}
        self.initialized = False
        self.workspace = Path.cwd()
        self.files_enabled = False

    def call(self, name, args=None):
        if self.loading_helper and not name.startswith("internal."):
            raise RuntimeError("Browser/workflow operations belong inside helper functions, not module initialization")
        if name == "internal.helpers.define":
            helper_name, source = args["name"], args["source"]
            tree = ast.parse(source)
            if not helper_name.isidentifier() or helper_name in ("api", "td"):
                raise ValueError("helper name must be a public Python identifier other than api/td")
            if helper_name != "agent_helpers" and not any(isinstance(node, ast.FunctionDef) and node.name == helper_name for node in tree.body):
                raise ValueError(f"Helper must define {helper_name}")
        self.sequence += 1
        identity = self.sequence
        self.wire_out.write(json.dumps({"type": "call", "id": identity, "name": name, "args": args or {},
                                       "parent": self.parents[-1] if self.parents else None,
                                       "callbackId": self.callback_stack[-1] if self.callback_stack else None}) + "\n")
        self.wire_out.flush()
        while identity not in self.pending:
            line = self.wire_in.readline()
            if not line:
                raise RuntimeError("Host disconnected; inspect the current page before retrying")
            response = json.loads(line)
            if response.get("type") == "callback":
                event = response["event"]
                self.callback_stack.append(event["id"])
                try:
                    with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
                        value = self.transport.callback(event)
                    self.wire_out.write(json.dumps({"type": "callback_result", "id": event["id"], "ok": True, "value": value}) + "\n")
                except BaseException as error:
                    self.wire_out.write(json.dumps({"type": "callback_result", "id": event["id"], "ok": False, "error": str(error)[:1000]}) + "\n")
                    if isinstance(error, Terminal):
                        raise
                finally:
                    self.callback_stack.pop()
                    self.wire_out.flush()
            else:
                self.pending[response["id"]] = response["result"]
        result = self.pending.pop(identity)
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

    def initialize(self, config):
        if not self.initialized:
            roots = self.transport.decode(self.call("playwright.open"))
            self.page, self.context = roots["page"], roots["context"]
            self.workflow = Workflow(self.transport, input=config.get("input"))
            self.browser = Workflow(self.transport, path="browser")
            install_sync_api(self.transport, self.page, self.context)
            browser_harness.page, browser_harness.context = self.page, self.context
            browser_harness.expect, browser_harness.workflow, browser_harness.browser = self.transport.expect, self.workflow, self.browser
            self.env.update(page=self.page, context=self.context, expect=self.transport.expect, workflow=self.workflow, browser=self.browser)
            files = config.get("workspace")
            self.files_enabled = files is not None
            if files is not None:
                for name in files:
                    item = self.call("internal.workspace.read", {"path": name})
                    destination = self.workspace / name
                    if not destination.resolve().is_relative_to(self.workspace.resolve()):
                        raise ValueError("Invalid workspace path")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(base64.b64decode(item["content"], validate=True)
                                            if item["encoding"] == "base64" else item["content"].encode())
            sys.path.insert(0, str(self.workspace))
            self.initialized = True
        # Input belongs to the current call even when user variables are retained.
        self.workflow.input = config.get("input")

    def wrap(self, fn, item):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            self.call("internal.helpers.use", {"name": item["name"], "revision": item["revision"]})
            self.parents.append({"name": item["name"], "revision": item["revision"]})
            try:
                return fn(*args, **kwargs)
            finally:
                self.parents.pop()
        return wrapped

    def install_helpers(self, config):
        for item in config.get("helpers", []):
            name, revision = item["name"], item["revision"]
            self.call("internal.helpers.use", {"name": name, "revision": revision})
            if self.helper_revisions.get(name) == revision:
                continue
            module = types.ModuleType(name)
            scope = module.__dict__
            scope.update(self.env)
            previous = sys.modules.get(name)
            sys.modules[name] = module
            self.loading_helper = True
            try:
                with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
                    exec(compile(item["source"], f"{name}.py", "exec"), scope)
                if name == "agent_helpers":
                    for key, value in scope.items():
                        if not key.startswith("_") and getattr(value, "__globals__", None) is scope and key not in self.env:
                            self.env[key] = self.wrap(value, item)
                else:
                    fn = scope.get(name)
                    if not callable(fn):
                        raise ValueError(f"Helper must define {name}")
                    self.env[name] = self.wrap(fn, item)
                self.helper_scopes[name] = scope
                self.helper_revisions[name] = revision
            except Exception as error:
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous
                print(f"Could not load helper {name}: {error}", file=self.output)
            finally:
                self.loading_helper = False

    def save_files(self):
        if not self.files_enabled:
            return
        files, total = {}, 0
        for item in sorted(self.workspace.rglob("*")):
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
            files[item.relative_to(self.workspace).as_posix()] = value
        self.call("internal.workspace.commit", {"files": files})

    def execute(self, config):
        # Compile before initialization: a syntax error must not touch the browser/files.
        try:
            program = compile(config["source"], "browser.py", "exec")
        except SyntaxError as error:
            message = f"Python syntax error on line {error.lineno}: {error.msg}. Nothing was executed."
            if "await" in error.msg or "async" in error.msg:
                message += " The browser API is synchronous: use print(page.title()) or page.locator('body').inner_text() without await or async def."
            return {"type": "result", "ok": False, "error": message}
        if config.get("compiled"):
            validate_compiled_source(config["source"])
            for helper in config.get("helpers", []):
                validate_compiled_source(helper["source"])
        self.initialize(config)
        self.install_helpers(config)
        library = next((h for h in config.get("helpers", []) if h["name"] == "agent_helpers"), None)
        helper_path = self.workspace / "agent_helpers.py"
        if not helper_path.exists() or library and helper_path.read_text() != library["source"]:
            helper_path.write_text(library["source"] if library else "")
        result = {"type": "result", "ok": True}
        try:
            with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
                exec(program, self.env)
                if config.get("compiled"):
                    self.env["run"](self.page, self.context, self.workflow)
        except Terminal:
            pass
        except BaseException as error:
            result = {"type": "result", "ok": False,
                      "error": str(error) if isinstance(error, (BrowserError, WorkflowError)) else traceback.format_exc(limit=8)[-4000:]}
        try:
            self.save_files()
            changed = helper_path.read_text()
            if not config.get("compiled") and changed != (library["source"] if library else ""):
                self.call("internal.helpers.define", {"name": "agent_helpers", "source": changed or "# empty helper module\n"})
        except BaseException:
            result = {"type": "result", "ok": False, "error": traceback.format_exc(limit=8)[-4000:]}
        if not config.get("repl"):
            try:
                self.call("playwright.close")
            except BaseException:
                pass
        return result


def main():
    wire_in, wire_out = sys.stdin, sys.stdout
    interpreter = Interpreter(wire_in, wire_out)
    scope = None
    for line in wire_in:
        config = json.loads(line)
        wire_out.write(json.dumps({"type": "ready", "protocolVersion": 4, "sdkVersion": "playwright-python-v1",
                                   "sessionId": interpreter.session_id}) + "\n")
        wire_out.flush()
        try:
            if scope is not None and config.get("scope") != scope:
                raise RuntimeError("Cannot reuse a Python REPL across run scopes")
            scope = config.get("scope")
            result = interpreter.execute(config)
        except BaseException:
            result = {"type": "result", "ok": False, "error": traceback.format_exc(limit=8)[-4000:]}
        wire_out.write(json.dumps(result) + "\n")
        wire_out.flush()
        if not config.get("repl"):
            break


if __name__ == "__main__":
    # --supervise remains the container entry point; one process now owns all cells.
    main()
