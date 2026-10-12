"""Synchronous Playwright objects over the recorded, run-owned host transport.

No browser connection or credential lives here. Signatures come from the pinned
Playwright manifest; unknown members cannot become arbitrary remote getattr calls.
"""
import base64
import inspect
import datetime
import mimetypes
import typing
import json
import math
import re
import sys
import types
from pathlib import Path

MANIFEST = json.loads(Path(__file__).with_name("playwright_manifest.json").read_text())


class BrowserError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message, self.name, self.stack = message, None, None


class BrowserTimeoutError(BrowserError):
    pass


class Proxy:
    def __init__(self, transport, reference):
        self._transport = transport
        self._reference = reference

    def __repr__(self):
        return f"<{self._reference['class']} remote>"

    def _method(self, name, spec):
        def method(*args, **kwargs):
            signature(spec).bind(*args, **kwargs)
            args, kwargs = list(args), dict(kwargs)
            # Paths are always local to the execution sandbox, never worker paths.
            if name in {"set_input_files", "set_files"}:
                args, kwargs = list(args), dict(kwargs)
                offset = 1 if self._reference["class"] in ("Page", "Frame") else 0
                files = args[offset] if len(args) > offset else kwargs.get("files")
                def upload(item):
                    if isinstance(item, (str, Path)):
                        p = Path(item)
                        if p.is_dir():
                            files, size = [], 0
                            for entry in sorted(p.rglob("*")):
                                if entry.is_file():
                                    data = entry.read_bytes()
                                    size += len(data)
                                    if size > 8_000_000 or len(files) >= 1000:
                                        raise ValueError("Directory upload exceeds the transfer budget")
                                    files.append({"path": entry.relative_to(p).as_posix(), "data": base64.b64encode(data).decode()})
                            return {"$directory": {"name": p.name, "files": files}}
                        return {"name": p.name, "mimeType": mimetypes.guess_type(p.name)[0] or "application/octet-stream", "buffer": p.read_bytes()}
                    return item
                files = [upload(f) for f in files] if isinstance(files, (list, tuple)) else upload(files)
                if len(args) > offset:
                    args[offset] = files
                else:
                    kwargs["files"] = files
            if name in {"add_script_tag", "add_style_tag", "add_init_script", "fulfill"} and kwargs.get("path") is not None:
                kwargs = dict(kwargs)
                source = Path(kwargs.pop("path"))
                key = "script" if name == "add_init_script" else "body" if name == "fulfill" else "content"
                kwargs[key] = source.read_bytes() if name == "fulfill" else source.read_text()
            destination = kwargs.pop("path", None) if name in {"screenshot", "pdf", "storage_state"} else None
            if name == "screenshot" and destination is not None and kwargs.get("type") is None:
                mime = mimetypes.guess_type(str(destination))[0]
                if mime not in {"image/png", "image/jpeg"}:
                    raise BrowserError("Screenshot path must have a PNG or JPEG extension, or supply type explicitly")
                kwargs["type"] = "jpeg" if mime == "image/jpeg" else "png"
            if name == "route_from_har":
                path = Path(args[0] if args else kwargs["har"])
                file = {"$file": {"name": path.name, "data": base64.b64encode(path.read_bytes()).decode()}}
                if args:
                    args[0] = file
                else:
                    kwargs["har"] = file
            if self._reference["class"] == "Tracing" and name in {"stop", "stop_chunk"} and kwargs.get("path") is not None:
                destination = kwargs["path"]
                data = self._invoke(name, [], {"path": "__transfer__"})
                write_file(destination, data)
                return None
            if name in {"save_as", "path"} and self._reference["class"] in {"Download", "Video"}:
                destination = (args[0] if args else kwargs["path"]) if name == "save_as" else Path(".playwright") / self._reference["id"]
                data = self._invoke("save_as", [], {"path": "__transfer__"})
                write_file(destination, data)
                return Path(destination) if name == "path" else None
            result = self._invoke(name, list(args), kwargs)
            if destination is not None:
                write_file(destination, json.dumps(result).encode() if name == "storage_state" else result)
            return result
        method.__name__ = name
        method.__doc__ = spec["signature"]
        method.__signature__ = signature(spec)
        return method

    def _invoke(self, member, args, kwargs):
        result = self._transport.call("playwright.call", {
            "target": self._reference, "member": member,
            "args": self._transport.encode(args), "kwargs": self._transport.encode(kwargs),
        })
        return self._transport.decode(result)

    def __enter__(self):
        if not MANIFEST["metadata"][self._reference["class"]]["contextManager"]:
            raise TypeError(f"{self._reference['class']} is not a context manager")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


def write_file(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def signature(spec, bound=True):
    parameters = [] if bound else [inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    parameters.extend(inspect.Parameter(p["name"], getattr(inspect.Parameter, p["kind"]),
                      default=p.get("default", inspect.Parameter.empty),
                      annotation=p.get("annotation") or inspect.Parameter.empty)
                      for p in spec["parameters"])
    return inspect.Signature(parameters)


def member_descriptor(name, spec):
    if spec["property"]:
        return property(lambda self: self._invoke(name, [], {}), doc=spec["signature"])
    def method(self, *args, **kwargs):
        return self._method(name, spec)(*args, **kwargs)
    method.__name__, method.__doc__ = name, spec["signature"]
    method.__signature__ = signature(spec, bound=False)
    return method


class EventInfo:
    def __init__(self, manager):
        self._manager, self._resolved, self._value = manager, False, None

    @property
    def value(self):
        if not self._resolved:
            self._value = self._manager._invoke("value", [], {})
            self._resolved = True
        return self._value

    def is_done(self):
        return self._resolved or self._manager._invoke("is_done", [], {})


class EventContextManager(Proxy):
    def __enter__(self):
        self._invoke("enter", [], {})
        self._event = EventInfo(self)
        return self._event

    def __exit__(self, exc_type, exc, tb):
        self._invoke("exit", [], {"cancel": exc_type is not None})
        if exc_type is None:
            self._event.value


# Build every public member and inheritance relationship from the pinned API.
# Class descriptors preserve introspection and unbound calls such as Page.title(page).
PUBLIC_TYPES = {}
def public_type(name):
    if name not in PUBLIC_TYPES:
        bases = tuple(public_type(base) for base in MANIFEST["metadata"][name]["bases"]) or (Proxy,)
        members = {member: member_descriptor(member, spec) for member, spec in MANIFEST["classes"][name].items()}
        PUBLIC_TYPES[name] = type(name, bases, {"__module__": "playwright.sync_api", **members})
    return PUBLIC_TYPES[name]
for _name in MANIFEST["classes"]:
    public_type(_name)
for _name, _target in MANIFEST["aliases"].items():
    PUBLIC_TYPES[_name] = PUBLIC_TYPES[_target]
STRUCTURES = {name: typing.TypedDict(name, spec["fields"], total=spec["total"])
              for name, spec in MANIFEST["structures"].items()}
for _structure in STRUCTURES.values():
    _structure.__module__ = "playwright.sync_api"
PUBLIC_EXPORTS = set(PUBLIC_TYPES) | set(STRUCTURES) | {"page", "context", "expect", "Expect", "Error", "TimeoutError"}
BrowserError.__name__ = BrowserError.__qualname__ = "Error"
BrowserError.__module__ = "playwright.sync_api"
BrowserTimeoutError.__name__ = BrowserTimeoutError.__qualname__ = "TimeoutError"
BrowserTimeoutError.__module__ = "playwright.sync_api"


class Expect:
    _unset = object()

    def __init__(self, transport):
        self._transport, self._timeout = transport, None

    def set_options(self, timeout=_unset):
        if timeout is not self._unset:
            self._timeout = timeout

    def __call__(self, actual, message=None):
        if not isinstance(actual, (PUBLIC_TYPES["Page"], PUBLIC_TYPES["Locator"], PUBLIC_TYPES["APIResponse"])):
            raise ValueError(f"Unsupported type: {type(actual)}")
        args = {"target": actual._reference, "message": message}
        if self._timeout is not None:
            args["timeout"] = self._timeout
        return self._transport.decode(self._transport.call("playwright.expect", args))


def install_sync_api(transport, page, context):
    """Install Playwright's synchronous API for the browser already owned by this run."""
    package = types.ModuleType("playwright")
    package.__path__ = []
    module = types.ModuleType("playwright.sync_api")
    module.__package__ = "playwright"
    module.__dict__.update(PUBLIC_TYPES)
    module.__dict__.update(STRUCTURES)
    module.page, module.context, module.expect = page, context, transport.expect
    class BoundExpect(Expect):
        def __init__(self):
            super().__init__(transport)
    BoundExpect.__name__ = BoundExpect.__qualname__ = "Expect"
    BoundExpect.__module__ = "playwright.sync_api"
    module.Expect = BoundExpect
    module.Error, module.TimeoutError = BrowserError, BrowserTimeoutError
    module.__all__ = sorted(PUBLIC_EXPORTS)
    package.sync_api = module
    sys.modules["playwright"] = package
    sys.modules["playwright.sync_api"] = module


class Transport:
    def __init__(self, call):
        self.call = call
        self.callbacks = {}
        self.callback_ids = {}
        self.objects = {}
        self.expect = Expect(self)

    def encode(self, value):
        if isinstance(value, Proxy):
            return {"$ref": value._reference}
        if isinstance(value, bytes):
            return {"$bytes": base64.b64encode(value).decode()}
        if isinstance(value, datetime.datetime):
            return {"$datetime": value.isoformat()}
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, re.Pattern):
            return {"$regex": value.pattern, "flags": value.flags}
        if isinstance(value, float) and not math.isfinite(value):
            return {"$number": str(value)}
        if callable(value):
            identity = self.callback_ids.get(id(value))
            if identity is None:
                identity = str(len(self.callbacks) + 1)
                self.callback_ids[id(value)] = identity
                self.callbacks[identity] = value
            return {"$callback": identity}
        if isinstance(value, (list, tuple)):
            return [self.encode(v) for v in value]
        if isinstance(value, dict):
            return {k: self.encode(v) for k, v in value.items()}
        return value

    def decode(self, value):
        if isinstance(value, list):
            return [self.decode(v) for v in value]
        if isinstance(value, dict):
            if "$error" in value:
                error = value["$error"]
                result = (BrowserTimeoutError if error["name"] == "TimeoutError" else BrowserError)(error["message"])
                result.name = error["name"]
                return result
            if "$ref" in value:
                ref = value["$ref"]
                key = (ref["scope"], ref["id"])
                if key not in self.objects:
                    cls = EventContextManager if ref["class"] == "EventContextManager" else PUBLIC_TYPES[ref["class"]]
                    self.objects[key] = cls(self, ref)
                return self.objects[key]
            if "$bytes" in value:
                return base64.b64decode(value["$bytes"], validate=True)
            if "$number" in value:
                return float(value["$number"])
            return {k: self.decode(v) for k, v in value.items()}
        return value

    def callback(self, event):
        fn = self.callbacks[event["callback"]]
        args = self.decode(event.get("args", []))
        try:
            parameters = list(inspect.signature(fn).parameters.values())
        except (TypeError, ValueError):
            parameters = [inspect.Parameter("args", inspect.Parameter.VAR_POSITIONAL)]
        if not any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in parameters):
            count = sum(p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD) for p in parameters)
            args = args[:count]
        result = fn(*args)
        if inspect.isawaitable(result):
            raise TypeError("Use synchronous callbacks with the synchronous browser API")
        return self.encode(result)

BROWSER_METHODS = {
    "emit", "emit.batch", "record.outcome",
    "memory.get", "memory.set", "history.read", "output.read",
    "secrets.fill", "done", "fail", "deopt", "describe", "yield_control",
    "ai", "resume",
}


class BrowserServices:
    def __init__(self, transport, path="", input=None):
        self._transport, self._path = transport, path
        if not path:
            self.input = input

    def __getattr__(self, name):
        path = f"{self._path}.{name}" if self._path else name
        if not any(p == path or p.startswith(path + ".") for p in BROWSER_METHODS):
            raise AttributeError(f"Unknown browser method: {path}")
        return BrowserServices(self._transport, path)

    def __call__(self, *args, **kwargs):
        if self._path not in BROWSER_METHODS:
            raise TypeError("Select a browser method")
        name = "browser." + self._path if self._path == "ai" else "workflow." + self._path
        if self._path == "ai" and args:
            if len(args) != 2 or kwargs:
                raise TypeError("browser.ai(prompt, schema_def) expects exactly two positional arguments")
            payload = {"prompt": args[0], "schema_def": args[1]}
        elif len(args) > 1:
            raise TypeError("Browser service methods accept at most one positional argument")
        else:
            payload = args[0] if args else kwargs
        return self._transport.decode(self._transport.call(name,
                                      self._transport.encode(payload)))
