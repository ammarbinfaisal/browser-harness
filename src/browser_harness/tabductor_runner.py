"""Isolated Python runner. All browser effects cross the recorded stdio gateway.

Run in the supplied networkless container, never in the trusted worker process.
stdout is protocol-only; user output goes to stderr. No browser credentials enter
this process. The host, not these messages, decides completion and write receipts.
"""
import contextlib
import ast
import functools
import json
import os
import queue
import signal
import sys
import threading
import traceback
import base64
import types
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import browser_harness
from browser_harness.playwright_proxy import (
    BrowserError, BrowserTimeoutError, BrowserServices, PUBLIC_EXPORTS, Transport, install_sync_api,
)


class Terminal(BaseException):
    pass


class ServiceError(Exception):
    """A rejected browser service call, not a bug in the submitted Python program."""


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
            if module == "browser_harness" and any(n.name not in {"expect", "page", "context", "browser"} for n in node.names):
                raise ValueError("Only public browser objects can be imported")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("Compiled programs cannot access runtime internals")
        if isinstance(node, ast.Name) and node.id in forbidden_calls:
            raise ValueError("Dynamic Python execution and introspection cannot be compiled")
    return tree



class HostInput:
    """Host frames, read on a thread so an interrupt can reach a cell that is computing.

    An interrupt raises KeyboardInterrupt in the cell (a real SIGINT also wakes sleeps and
    blocking waits); outside a cell it is ignored, so it can never end the kernel itself.
    """
    def __init__(self, stream):
        self.lines = queue.Queue()
        self.executing = False
        threading.Thread(target=self.read, args=(stream,), daemon=True).start()

    def read(self, stream):
        for line in stream:
            if '"interrupt"' in line:
                try:
                    frame = json.loads(line)
                except ValueError:
                    frame = None
                if isinstance(frame, dict) and frame.get("type") == "interrupt":
                    if self.executing:
                        os.kill(os.getpid(), signal.SIGINT)
                    continue
            self.lines.put(line)
        self.lines.put("")

    def readline(self):
        return self.lines.get()

    def __iter__(self):
        while line := self.readline():
            yield line


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
            raise RuntimeError("Browser operations belong inside helper functions, not module initialization")
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
                error_type = ServiceError if name.startswith(("workflow.", "browser.")) else RuntimeError
            raise error_type(result.get("error", "Operation failed"))
        return result.get("value")

    def initialize(self, config):
        if not self.initialized:
            roots = self.transport.decode(self.call("playwright.open"))
            self.page, self.context = roots["page"], roots["context"]
            self.browser = BrowserServices(self.transport, input=config.get("input"))
            install_sync_api(self.transport, self.page, self.context)
            browser_harness.page, browser_harness.context = self.page, self.context
            browser_harness.expect, browser_harness.browser = self.transport.expect, self.browser
            self.env.update(page=self.page, context=self.context, expect=self.transport.expect, browser=self.browser)
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
        self.browser.input = config.get("input")

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
        # Only compiled artifacts pin helpers now (saved by the retired browser.pyfunc tool);
        # AI cells define functions directly in the persistent namespace.
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
        before = {key: id(value) for key, value in self.env.items()}
        result = {"type": "result", "ok": True}
        interruptible = isinstance(self.wire_in, HostInput)
        try:
            with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
                if interruptible:
                    self.wire_in.executing = True
                try:
                    exec(program, self.env)
                    if config.get("compiled"):
                        self.env["run"](self.page, self.context, self.browser)
                finally:
                    if interruptible:
                        self.wire_in.executing = False
        except Terminal:
            pass
        except BaseException as error:
            result = {"type": "result", "ok": False,
                      "error": str(error) if isinstance(error, (BrowserError, ServiceError)) else traceback.format_exc(limit=8)[-4000:]}
        # Names this cell bound or rebound at top level, even when it later raised: the
        # namespace persists across cells, and the compiler needs to know where names came from.
        result["namespace"] = {"defined": sorted(key for key, value in self.env.items()
                                                 if not key.startswith("_") and before.get(key) != id(value))[:200]}
        try:
            self.save_files()
        except BaseException:
            result = {"type": "result", "ok": False, "error": traceback.format_exc(limit=8)[-4000:], "namespace": result["namespace"]}
        if not config.get("repl"):
            try:
                self.call("playwright.close")
            except BaseException:
                pass
        return result


def main():
    wire_in, wire_out = HostInput(sys.stdin), sys.stdout
    interpreter = Interpreter(wire_in, wire_out)
    scope = None
    for line in wire_in:
        config = json.loads(line)
        wire_out.write(json.dumps({"type": "ready", "protocolVersion": 5, "sdkVersion": "playwright-python-v1",
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
    # --supervise remains the container entry point; with repl=true one process owns all of a run's cells.
    main()
