"""Isolated Python runner. All browser effects cross the recorded stdio gateway.

Run in the supplied networkless container, never in the trusted worker process.
stdout is protocol-only; user output goes to stderr. No browser credentials enter
this process. The host, not these messages, decides completion and write receipts.
"""
import contextlib
import functools
import json
import os
import queue
import re
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
    BrowserError, BrowserTimeoutError, BrowserServices, Transport, install_sync_api,
)


class Terminal(BaseException):
    pass


class ServiceError(Exception):
    """A rejected browser service call, not a bug in the submitted Python program."""


class ScopeDeopt(Exception):
    """A compiled block's guard did not hold: raised by Scope.deopt, handled by Scope.__exit__."""
    def __init__(self, reason, evidence=None):
        super().__init__(reason)
        self.reason, self.evidence = reason, evidence


SCOPE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
SCOPE_DEPTH = 3


class Scope:
    """One block of a compiled module that an AI agent may finish in place.

    An exception inside the block, or scope.deopt(), hands only this block to the agent: the
    host runs the agent's cells in this kernel, over the block's variables, until
    browser.resume(value=...) supplies what the rest of the script needs. Only `value` flows
    back; the script continues after the block. When the agent cannot finish the block the host
    answers with a terminal and the whole run is handed over, as browser.deopt always did.
    """
    def __init__(self, interp, id, goal, returns):
        if not isinstance(id, str) or not SCOPE_ID.match(id):
            raise ValueError("browser.scope id must be a short kebab-case ledger step id")
        if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 600:
            raise ValueError("browser.scope goal must be 1-600 characters")
        if returns is not None and not isinstance(returns, dict):
            raise ValueError("browser.scope returns must be a JSON Schema object or None")
        self.interp, self.id, self.goal, self.returns = interp, id, goal.strip(), returns
        self.value, self.resumed, self.active = None, False, False

    def deopt(self, reason, evidence=None):
        if not self.active:
            raise RuntimeError("scope.deopt is only valid inside its with block")
        raise ScopeDeopt(str(reason), evidence)

    def __enter__(self):
        interp = self.interp
        if not interp.compiled_cell or interp.cell_stack:
            raise RuntimeError("browser.scope belongs to compiled modules; inside a recovery cell finish the block and call browser.resume(value=...)")
        if len(interp.scope_stack) >= SCOPE_DEPTH:
            raise RuntimeError(f"browser.scope nests at most {SCOPE_DEPTH} deep")
        interp.scope_stack.append(self.id)
        self.active, self.entered = True, interp.sequence
        return self

    def __exit__(self, exc_type, exc, tb):
        self.active = False
        self.interp.scope_stack.pop()
        if exc is None or isinstance(exc, (Terminal, KeyboardInterrupt, SystemExit, GeneratorExit)):
            return False
        if isinstance(exc, ScopeDeopt):
            cause, reason, error = "guard", exc.reason, None
        else:
            cause = "error"
            reason = f"{type(exc).__name__}: {str(exc)[:500]}"
            error = str(exc) if isinstance(exc, (BrowserError, ServiceError)) \
                else "".join(traceback.format_exception(exc_type, exc, tb, limit=8))[-4000:]
        # The traceback starts at the frame executing the with statement: the block's variables.
        snapshot = {}
        if tb is not None:
            for name, value in dict(tb.tb_frame.f_locals).items():
                if name.startswith("__") or value is self or name in ("page", "context", "browser"):
                    continue
                snapshot[name] = value
        namespace = dict(self.interp.env)
        namespace.update(snapshot)
        key = f"{self.id}#{self.interp.scope_sequence}"
        self.interp.scope_sequence += 1
        self.interp.scope_namespaces[key] = (namespace, sorted(snapshot))
        summary = []
        for name, value in list(snapshot.items())[:60]:
            try:
                shown = repr(value)[:160]
            except Exception:
                shown = "<unrepresentable>"
            summary.append({"name": name, "type": type(value).__name__, "repr": shown})
        try:
            result = self.interp.call("workflow.scope.recover", {
                "scopeId": self.id, "scopeKey": key, "goal": self.goal, "returns": self.returns,
                "reason": reason, "cause": cause,
                "evidence": self.interp.transport.encode(getattr(exc, "evidence", None)),
                "error": error, "locals": summary, "depth": len(self.interp.scope_stack) + 1,
                "since": self.entered,
            })
        finally:
            self.interp.scope_namespaces.pop(key, None)
        self.value = self.interp.transport.decode(result.get("value") if isinstance(result, dict) else None)
        self.resumed = True
        return True


class HostInput:
    """Host frames, read on a thread so an interrupt can reach a cell that is computing.

    An interrupt raises KeyboardInterrupt in the cell (a real SIGINT also wakes sleeps and
    blocking waits); outside a cell it is ignored, so it can never end the kernel itself. An
    interrupt naming a recovery cell reaches only that cell.
    """
    def __init__(self, stream):
        self.lines = queue.Queue()
        self.executing = False
        self.executing_cell = None
        threading.Thread(target=self.read, args=(stream,), daemon=True).start()

    def read(self, stream):
        for line in stream:
            if '"interrupt"' in line:
                try:
                    frame = json.loads(line)
                except ValueError:
                    frame = None
                if isinstance(frame, dict) and frame.get("type") == "interrupt":
                    cell = frame.get("cell")
                    if self.executing and (cell is None or cell == self.executing_cell):
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
    """Output shares the protocol stream so a result cannot overtake printed text. Text printed
    by a recovery cell names that cell, so the host routes it to the agent, not the module."""
    def __init__(self, wire, cell_of=lambda: None):
        self.wire, self.cell_of = wire, cell_of

    def write(self, text):
        if text:
            frame = {"type": "output", "text": text}
            cell = self.cell_of()
            if cell is not None:
                frame["cell"] = cell
            self.wire.write(json.dumps(frame) + "\n")
            self.wire.flush()
        return len(text)

    def flush(self):
        self.wire.flush()


class Interpreter:
    """One namespace, transport and browser-object scope for a run-owned REPL."""
    def __init__(self, wire_in, wire_out):
        self.wire_in, self.wire_out = wire_in, wire_out
        self.session_id = str(uuid.uuid4())
        self.output = CellOutput(wire_out, lambda: self.cell_stack[-1] if self.cell_stack else None)
        self.sequence, self.parents = 0, []
        self.callback_stack, self.pending = [], {}
        # Scoped recovery: the open browser.scope ids, the recovery cells executing (innermost
        # last), and per pending recovery the namespace its cells run in.
        self.compiled_cell, self.scope_stack, self.cell_stack = False, [], []
        self.scope_namespaces, self.scope_sequence = {}, 0
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
                                       "callbackId": self.callback_stack[-1] if self.callback_stack else None,
                                       "scope": self.scope_stack[-1] if self.scope_stack else None,
                                       "cell": self.cell_stack[-1] if self.cell_stack else None}) + "\n")
        self.wire_out.flush()
        while identity not in self.pending:
            line = self.wire_in.readline()
            if not line:
                raise RuntimeError("Host disconnected; inspect the current page before retrying")
            response = json.loads(line)
            if response.get("type") == "cell":
                # A recovery cell, run while this call (workflow.scope.recover) waits for its answer.
                self.run_nested_cell(response)
            elif response.get("type") == "callback":
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
        # A terminal answer ends the cell: completion, handoff, yield, a recovery's resume, or a
        # scope recovery the host escalated to a whole-run handoff.
        if result.get("ok") and (name in ("workflow.done", "workflow.fail", "workflow.deopt", "workflow.yield_control", "workflow.resume")
                                 or result.get("terminal")):
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
            # An instance attribute: BrowserServices.__getattr__ never sees it, and it needs this
            # interpreter, not an RPC.
            self.browser.scope = lambda id, *, goal, returns=None: Scope(self, id, goal, returns)
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

    def run_nested_cell(self, frame):
        """One agent cell inside a scope recovery, in the namespace Scope.__exit__ prepared:
        the module's globals plus the failed block's variables. Nothing it binds survives the
        recovery except through browser.resume(value=...)."""
        identity, source = frame.get("id"), frame.get("source", "")
        entry = self.scope_namespaces.get(frame.get("scope"))
        reply = {"type": "cell_result", "id": identity, "ok": True, "namespace": {"defined": [], "inherited": []}}
        program = None
        if entry is None:
            reply.update(ok=False, error="No open scope recovery accepts cells")
        else:
            try:
                program = compile(source, "recovery.py", "exec")
            except SyntaxError as error:
                reply.update(ok=False, error=syntax_message(error))
        if program is not None:
            namespace, inherited = entry
            before = {key: id(value) for key, value in namespace.items()}
            self.cell_stack.append(identity)
            if isinstance(self.wire_in, HostInput):
                self.wire_in.executing_cell = identity
            try:
                exec(program, namespace)
            except Terminal:
                reply["terminal"] = True
            except KeyboardInterrupt:
                reply.update(ok=False, interrupted=True, error="The cell was interrupted")
            except BaseException as error:
                reply.update(ok=False, error=str(error) if isinstance(error, (BrowserError, ServiceError))
                             else traceback.format_exc(limit=8)[-4000:])
            finally:
                self.cell_stack.pop()
                if isinstance(self.wire_in, HostInput):
                    self.wire_in.executing_cell = self.cell_stack[-1] if self.cell_stack else None
            reply["namespace"] = {"defined": sorted(key for key, value in namespace.items()
                                                    if not key.startswith("_") and before.get(key) != id(value))[:200],
                                  "inherited": inherited[:200]}
            try:
                self.save_files()
            except BaseException:
                reply.update(ok=False, error=traceback.format_exc(limit=8)[-4000:])
        self.wire_out.write(json.dumps(reply) + "\n")
        self.wire_out.flush()

    def execute(self, config):
        # Compile before initialization: a syntax error must not touch the browser/files.
        try:
            program = compile(config["source"], "browser.py", "exec")
        except SyntaxError as error:
            return {"type": "result", "ok": False, "error": syntax_message(error)}
        self.initialize(config)
        self.install_helpers(config)
        self.compiled_cell = bool(config.get("compiled"))
        self.scope_stack, self.cell_stack = [], []
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


def syntax_message(error):
    message = f"Python syntax error on line {error.lineno}: {error.msg}. Nothing was executed."
    if "await" in error.msg or "async" in error.msg:
        message += " The browser API is synchronous: use print(page.title()) or page.locator('body').inner_text() without await or async def."
    return message


def main():
    wire_in, wire_out = HostInput(sys.stdin), sys.stdout
    interpreter = Interpreter(wire_in, wire_out)
    scope = None
    for line in wire_in:
        config = json.loads(line)
        wire_out.write(json.dumps({"type": "ready", "protocolVersion": 6, "sdkVersion": "playwright-python-v1",
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
