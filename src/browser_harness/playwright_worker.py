"""Worker-owned Playwright objects and asynchronous callback exchange.

Only this module sees real Playwright objects. A scope is bound to an owned root
page and one Python invocation. Commands remain authenticated by the embedding
worker; callback tokens permit reentrancy only while that callback is pending.
"""
import asyncio
import base64
import contextvars
import datetime
import tempfile
import inspect
import json
import math
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse

MANIFEST = json.loads(Path(__file__).with_name("playwright_manifest.json").read_text())
CURRENT_JOB = contextvars.ContextVar("proxy_job", default=None)


class EventExpectation:
    def __init__(self, manager):
        self.manager, self.info = manager, None

    async def enter(self):
        self.info = await self.manager.__aenter__()

    async def exit(self, cancel=False):
        await self.manager.__aexit__(RuntimeError if cancel else None,
                                     RuntimeError("Cell abandoned expectation") if cancel else None, None)

    async def value(self):
        return await self.info.value

    def is_done(self):
        return self.info is not None and self.info._future.done()


class PredicateWaiter:
    """Playwright predicates are synchronous; remote predicates must be awaited."""
    def __init__(self, target, event, predicate, timeout):
        self.target, self.event, self.predicate = target, event, predicate
        self.future = asyncio.get_running_loop().create_future()
        self.timeout = min(max(float(timeout or 120000), 1), 120000) / 1000
        async def handler(value):
            try:
                if not self.future.done() and await predicate(value):
                    self.future.set_result(value)
            except BaseException as error:
                if not self.future.done():
                    self.future.set_exception(error)
        self.handler = handler
        target.on(event, handler)

    @property
    def _future(self):
        return self.future

    @property
    def value(self):
        return self.future

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if exc_type:
                self.future.cancel()
            else:
                await asyncio.wait_for(asyncio.shield(self.future), self.timeout)
        finally:
            self.target.remove_listener(self.event, self.handler)


class Scope:
    def __init__(self, context, root, invocation, owns, on_task=None):
        self.context, self.root, self.invocation, self.owns = context, root, invocation, owns
        self.objects, self.identities, self.origins = {}, {}, {}
        self.jobs, self.events, self.callbacks, self.callback_functions = {}, [], {}, {}
        self.subscriptions, self.routes, self.bindings = [], [], []
        self.websocket_routes, self.dialog_expectations = [], []
        self.context_routes = []
        self.pages_ready = asyncio.Event()
        self.pages_ready.set()
        self.locator_handlers = []
        self.har_routes = []
        self.files = tempfile.TemporaryDirectory(prefix="playwright-cell-")
        self.transfer_paths = set()
        self.lock = asyncio.Lock()
        self.closed = False
        self.on_task = on_task
        self.initial_page = self.encode(root, root)
        self.initial_context = self.encode(context, root)

    def ref(self, value):
        if not isinstance(value, dict) or value.get("scope") != self.invocation or value.get("id") not in self.objects:
            raise ValueError("Expired or foreign Playwright object")
        obj = self.objects[value["id"]]
        if self.class_name(obj) != value.get("class"):
            raise ValueError("Playwright object type mismatch")
        return obj

    @staticmethod
    def class_name(value):
        return "EventContextManager" if isinstance(value, EventExpectation) else type(value).__name__

    def encode(self, value, origin=None):
        if isinstance(value, BaseException):
            return {"$error": {"name": type(value).__name__, "message": "Browser event reported an error."}}
        if isinstance(value, bytes):
            if len(value) > 8_000_000:
                raise ValueError("Browser transfer exceeds 8 MB")
            return {"$bytes": base64.b64encode(value).decode()}
        if isinstance(value, float) and not math.isfinite(value):
            return {"$number": str(value)}
        if isinstance(value, (tuple, list)):
            return [self.encode(v, origin) for v in value]
        if isinstance(value, dict):
            return {k: self.encode(v, origin) for k, v in value.items()}
        cls = self.class_name(value)
        if cls in MANIFEST["classes"] or cls == "EventContextManager":
            identity = self.identities.get(id(value))
            if identity is None:
                if len(self.objects) >= 10000:
                    raise ValueError("Playwright object budget exhausted")
                identity = str(uuid.uuid4())
                self.identities[id(value)] = identity
                self.objects[identity] = value
                self.origins[identity] = value if cls == "Page" else origin or self.root
            return {"$ref": {"id": identity, "class": cls, "scope": self.invocation}}
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        raise TypeError(f"Unsupported browser result: {cls}")

    def decode(self, value):
        if isinstance(value, list):
            return [self.decode(v) for v in value]
        if isinstance(value, dict):
            if "$ref" in value:
                return self.ref(value["$ref"])
            if "$datetime" in value:
                return datetime.datetime.fromisoformat(value["$datetime"])
            if "$directory" in value:
                directory = value["$directory"]
                root = Path(self.files.name) / str(uuid.uuid4()) / Path(directory["name"]).name
                root.mkdir(parents=True)
                size = 0
                if len(directory["files"]) > 1000:
                    raise ValueError("Directory upload exceeds the transfer budget")
                for entry in directory["files"]:
                    path = root / entry["path"]
                    if not path.resolve().is_relative_to(root.resolve()):
                        raise ValueError("Invalid directory upload path")
                    data = base64.b64decode(entry["data"], validate=True)
                    size += len(data)
                    if size > 8_000_000:
                        raise ValueError("Directory upload exceeds the transfer budget")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                self.transfer_paths.add(str(root))
                return str(root)
            if "$file" in value:
                file = value["$file"]
                data = base64.b64decode(file["data"], validate=True)
                if len(data) > 8_000_000:
                    raise ValueError("Browser transfer exceeds 8 MB")
                path = Path(self.files.name) / (str(uuid.uuid4()) + "-" + Path(file["name"]).name)
                path.write_bytes(data)
                self.transfer_paths.add(str(path))
                return str(path)
            if "$bytes" in value:
                data = base64.b64decode(value["$bytes"], validate=True)
                if len(data) > 8_000_000:
                    raise ValueError("Browser transfer exceeds 8 MB")
                return data
            if "$regex" in value:
                return re.compile(value["$regex"], value.get("flags", 0))
            if "$number" in value:
                return float(value["$number"])
            if "$callback" in value:
                identity = value["$callback"]
                if identity not in self.callback_functions:
                    async def callback(*args):
                        if self.closed:
                            raise RuntimeError("Python invocation ended")
                        ticket = str(uuid.uuid4())
                        future = asyncio.get_running_loop().create_future()
                        self.callbacks[ticket] = future
                        self.events.append({"id": ticket, "callback": identity, "args": self.encode(args),
                                            "parentJob": CURRENT_JOB.get()})
                        try:
                            return await asyncio.wait_for(future, 120)
                        finally:
                            self.callbacks.pop(ticket, None)
                    self.callback_functions[identity] = callback
                return self.callback_functions[identity]
            return {k: self.decode(v) for k, v in value.items()}
        return value

    async def owned_pages(self):
        return [p for p in self.context.pages if await self.owns(p)]

    async def require_exclusive_context(self):
        for page in self.context.pages:
            if not page.is_closed() and not await self.owns(page):
                raise ValueError("This context-wide operation would affect another task's page")

    async def invoke(self, request):
        target = self.ref(request["target"])
        cls, member = self.class_name(target), request["member"]
        spec = MANIFEST["classes"].get(cls, {}).get(member)
        if cls == "EventContextManager" and member in {"enter", "exit", "value", "is_done"}:
            spec = {"property": False}
        if spec is None:
            raise ValueError(f"Unavailable Playwright member: {cls}.{member}")
        origin = self.origins[request["target"]["id"]]
        if not await self.owns(origin):
            raise ValueError("Page no longer belongs to this run")
        args, kwargs = self.decode(request.get("args", [])), self.decode(request.get("kwargs", {}))
        if "parameters" in spec:
            inspect.Signature([inspect.Parameter(p["name"], getattr(inspect.Parameter, p["kind"]),
                               default=p.get("default", inspect.Parameter.empty))
                               for p in spec["parameters"]]).bind(*args, **kwargs)
        # Normalize positional-or-keyword arguments so all special cases accept
        # the same calls as native Playwright (including predicate= and event=).
        positional = [p for p in spec.get("parameters", []) if p["kind"] in {"POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD"}]
        for index, parameter in enumerate(positional[len(args):], start=len(args)):
            name = parameter["name"]
            if name in kwargs:
                args.append(kwargs.pop(name))
            elif any(p["name"] in kwargs for p in positional[index + 1:]):
                args.append(parameter.get("default"))
            else:
                break
        def argument(name, default=None):
            for index, parameter in enumerate(positional):
                if parameter["name"] == name and index < len(args):
                    return args[index]
            return kwargs.get(name, default)
        if "timeout" in kwargs and kwargs["timeout"] is not None:
            kwargs["timeout"] = min(max(float(kwargs["timeout"] or 120000), 1), 120000)
        # Lifecycle is owned by the host; exposing the native API must not let a
        # task close the shared browser, launch one, or escape its leased context.
        if cls in {"Playwright", "APIRequest", "Selectors", "BrowserType"} and not spec.get("property"):
            raise ValueError("Browser lifecycle is managed by the host; use the supplied page and context")
        if cls == "Browser" and member not in {"version", "is_connected", "contexts", "browser_type", "on", "once", "remove_listener"}:
            raise ValueError("Browser lifecycle is managed by the host; use the supplied page and context")
        if cls == "Browser" and member == "contexts":
            return self.encode([self.context], origin)
        if cls == "BrowserContext" and member == "close" or cls == "APIRequestContext" and member == "dispose":
            raise ValueError("The host owns the lifetime of the shared browser context")
        output_path = cls == "Tracing" and member in {"stop", "stop_chunk"} and kwargs.get("path") == "__transfer__"
        if "path" in kwargs and member not in {"save_as", "clear_cookies"} and not output_path:
            raise ValueError("Worker filesystem paths are not available")
        if cls in {"Tracing", "Clock"}:
            await self.require_exclusive_context()
        if output_path:
            path = Path(self.files.name) / (str(uuid.uuid4()) + ".zip")
            await getattr(target, member)(path=str(path))
            return self.encode(path.read_bytes(), origin)
        if member == "route_from_har":
            if str(argument("har")) not in self.transfer_paths:
                raise ValueError("HAR files must be transferred from the Python workspace")
            if kwargs.get("update"):
                raise ValueError("Updating HAR files requires closing the host-owned context; use read-only HAR routing")
        if member in {"set_input_files", "set_files"}:
            offset = 1 if cls in {"Page", "Frame"} else 0
            files = args[offset] if len(args) > offset else kwargs.get("files")
            files = files if isinstance(files, list) else [files]
            if any(not (isinstance(f, dict) and isinstance(f.get("buffer"), bytes))
                   and not (isinstance(f, str) and f in self.transfer_paths and Path(f).is_dir()) for f in files):
                raise ValueError("Uploads must transfer sandbox bytes, never worker paths")
        if member in {"goto", "add_script_tag", "add_style_tag"}:
            url = args[0] if member == "goto" and args else kwargs.get("url")
            if url and urlparse(url).scheme not in {"http", "https", "data"} and url != "about:blank":
                raise ValueError("Navigation to worker files or browser administration is unavailable")
        if member in {"save_as", "path"} and cls in {"Download", "Video"}:
            path = Path(self.files.name) / str(uuid.uuid4())
            await target.save_as(str(path))
            return self.encode(path.read_bytes(), origin)
        if cls == "BrowserContext":
            if member in {"pages", "background_pages"}:
                pages = await self.owned_pages() if member == "pages" else [p for p in target.background_pages if await self.owns(p)]
                return self.encode(pages, origin)
            scoped = {"pages", "background_pages", "browser", "new_page", "request", "on", "once", "remove_listener",
                      "expect_page", "expect_event", "wait_for_event", "expect_console_message", "route", "unroute", "unroute_all",
                      "set_default_timeout", "set_default_navigation_timeout"}
            if member not in scoped:
                await self.require_exclusive_context()
            if member == "new_page":
                self.pages_ready.clear()
                try:
                    page = await self.context.new_page()
                    page._tabductor_root = self.root
                    return self.encode(page, origin)
                finally:
                    self.pages_ready.set()
            if member == "request":
                return self.encode(self.context.request, origin)
            if member in {"set_default_timeout", "set_default_navigation_timeout"}:
                for p in await self.owned_pages():
                    getattr(p, member)(min(max(float(args[0] or 120000), 1), 120000))
                return None
        if cls == "APIRequestContext":
            if member == "storage_state":
                await self.require_exclusive_context()
            if member in {"get", "post", "put", "patch", "delete", "head", "fetch"}:
                address = args[0] if args else kwargs.get("url", kwargs.get("url_or_request", ""))
                if not isinstance(address, str):
                    address = address.url
                url = urljoin(origin.url, address)
                parsed, source = urlparse(url), urlparse(origin.url)
                if (parsed.scheme, parsed.netloc) != (source.scheme, source.netloc) or parsed.scheme not in ("http", "https"):
                    raise ValueError("Requests must remain on the owned page's origin")
                if args:
                    args[0] = url
                else:
                    kwargs["url_or_request" if member == "fetch" else "url"] = url
                kwargs["max_redirects"] = 0
        if member in {"on", "once", "remove_listener"}:
            event, handler = args if args else (kwargs["event"], kwargs["f"])
            if member == "remove_listener":
                for obj, evt, original, wrapped in list(self.subscriptions):
                    if obj is target and evt == event and original is handler:
                        obj.remove_listener(evt, wrapped)
                        self.subscriptions.remove((obj, evt, original, wrapped))
                        if evt == "dialog":
                            obj._proxy_dialog_listeners = max(0, getattr(obj, "_proxy_dialog_listeners", 0) - 1)
                return None
            async def wrapped(*values):
                value = values[0] if values else None
                page = value if self.class_name(value) == "Page" else origin
                if cls == "BrowserContext":
                    await self.pages_ready.wait()
                    try:
                        page = value if self.class_name(value) == "Page" else value.page if hasattr(value, "page") else value.frame.page
                    except Exception:
                        return
                if await self.owns(page):
                    await handler(*values)
            getattr(target, member)(event, wrapped)
            if event == "dialog" and cls == "BrowserContext":
                for owned_page in await self.owned_pages():
                    owned_page._proxy_dialog_listeners = getattr(owned_page, "_proxy_dialog_listeners", 0) + 1
                    self.dialog_expectations.append(owned_page)
            if event == "dialog":
                target._proxy_dialog_listeners = getattr(target, "_proxy_dialog_listeners", 0) + 1
            self.subscriptions.append((target, event, handler, wrapped))
            return None
        if cls in {"Page", "BrowserContext"} and member in {"route", "unroute", "unroute_all"}:
            url, handler = argument("url"), argument("handler")
            if member == "route":
                pattern = "**/*" if callable(url) else url
                remaining = kwargs.get("times")
                async def scoped_route(route, request):
                    nonlocal remaining
                    try:
                        owned = cls == "Page" or await self.owns(request.frame.page)
                    except Exception:
                        owned = False
                    matched = owned and (not callable(url) or await url(request.url))
                    if not matched or remaining is not None and remaining <= 0:
                        await route.fallback()
                        return
                    if remaining is not None:
                        remaining -= 1
                    await handler(route, request)
                await target.route(pattern, scoped_route)
                self.context_routes.append((target, url, handler, pattern, scoped_route))
                self.routes.append((target, "route", [pattern, scoped_route], {}))
            elif member == "unroute_all" and cls == "Page":
                await target.unroute_all(**kwargs)
                self.context_routes = [entry for entry in self.context_routes if entry[0] is not target]
            else:
                for entry in list(self.context_routes):
                    obj, original_url, original, pattern, wrapped = entry
                    if obj is target and (member == "unroute_all" or original_url == url and (handler is None or handler is original)):
                        await target.unroute(pattern, wrapped)
                        self.context_routes.remove(entry)
            return None
        if member == "wait_for_url" and callable(argument("url")):
            matcher = argument("url")
            frame = target.main_frame if cls == "Page" else target
            async def matched(candidate):
                return candidate is frame and await matcher(candidate.url)
            # Attach before reading the current URL to avoid losing a navigation.
            waiter = PredicateWaiter(frame.page, "framenavigated", matched, kwargs.get("timeout", 30000))
            if await matcher(frame.url):
                waiter.future.set_result(frame)
            async with waiter:
                pass
            if kwargs.get("wait_until") != "commit":
                await frame.wait_for_load_state(kwargs.get("wait_until") or "load", timeout=kwargs.get("timeout"))
            return None
        predicate = argument("predicate")
        if cls == "BrowserContext" and member in {"expect_event", "wait_for_event", "expect_page", "expect_console_message"}:
            original_predicate = predicate
            async def scoped_predicate(value):
                await self.pages_ready.wait()
                try:
                    page = value if self.class_name(value) == "Page" else value.page if hasattr(value, "page") else value.frame.page
                    if not await self.owns(page):
                        return False
                    return await original_predicate(value) if original_predicate else True
                except (AttributeError, RuntimeError):
                    return False
            predicate = scoped_predicate
        if member in {"expect_request", "expect_response"}:
            candidate = args[0] if args else kwargs.get("url_or_predicate")
            if callable(candidate):
                predicate = candidate
        if callable(predicate) and (member.startswith("expect_") or member == "wait_for_event"):
            event = {"expect_request_finished": "requestfinished", "expect_file_chooser": "filechooser", "expect_console_message": "console"}.get(member, member.removeprefix("expect_"))
            if member in {"expect_event", "wait_for_event"}:
                event = args[0] if args else kwargs["event"]
            waiter = PredicateWaiter(target, event, predicate, kwargs.get("timeout", 30000))
            if member == "wait_for_event":
                async with waiter:
                    value = await waiter.value
                return self.encode(value, origin)
            return self.encode(EventExpectation(waiter), origin)
        if member == "expect_event" and (args[0] if args else kwargs.get("event")) == "dialog":
            target._proxy_dialog_listeners = getattr(target, "_proxy_dialog_listeners", 0) + 1
            self.dialog_expectations.append(target)
        if member in {"expose_function", "expose_binding"}:
            name = args[0] if args else kwargs["name"]
            callback = args[1] if len(args) > 1 else kwargs["callback"]
            owned = getattr(target, "_proxy_bindings", {})
            if name in owned:
                target._impl_obj._bindings[name] = (lambda source, *values: callback(*values)) if member == "expose_function" else callback
                owned[name] = self.invocation
                self.bindings.append((target, name))
                return None
        if member == "route_web_socket" and callable(argument("url")):
            matcher, handler = argument("url"), argument("handler")
            async def matched_websocket(socket):
                if await matcher(socket.url):
                    await handler(socket)
                else:
                    await socket.connect_to_server()
            args = ["**/*", matched_websocket]
        before_routes = list(target._impl_obj._web_socket_routes) if member == "route_web_socket" else []
        before_har_routes = list(target._impl_obj._routes) if member == "route_from_har" else []
        before_har_routers = list(target._impl_obj._har_routers) if member == "route_from_har" else []
        if member == "add_locator_handler":
            self.locator_handlers.append((target, args[0]))
        fn = getattr(target, member)
        if spec.get("property"):
            result = fn
        else:
            # Bound Python signatures are the worker's authoritative argument check.
            inspect.signature(fn).bind(*args, **kwargs)
            if member.startswith("expect_"):
                return self.encode(EventExpectation(fn(*args, **kwargs)), origin)
            result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        if member == "route_web_socket":
            self.websocket_routes.extend((target, route) for route in target._impl_obj._web_socket_routes if route not in before_routes)
        if member == "route_from_har":
            self.har_routes.append((target, [r for r in target._impl_obj._routes if r not in before_har_routes],
                                    [r for r in target._impl_obj._har_routers if r not in before_har_routers]))
        if member in {"route", "route_web_socket"}:
            self.routes.append((target, member, args, kwargs))
        if member in {"expose_function", "expose_binding"}:
            name = args[0] if args else kwargs["name"]
            if not hasattr(target, "_proxy_bindings"):
                target._proxy_bindings = {}
            target._proxy_bindings[name] = self.invocation
            self.bindings.append((target, name))
        return self.encode(result, origin)

    def start(self, request, ticket):
        if self.closed or ticket in self.jobs:
            raise ValueError("Invocation ended or command already submitted")
        callback = request.get("callback")
        if callback and callback not in self.callbacks:
            raise ValueError("Callback is not pending")
        async def execute():
            token = CURRENT_JOB.set(request.get("operationId", ticket))
            try:
                if callback:
                    return await self.invoke(request)
                async with self.lock:
                    return await self.invoke(request)
            finally:
                CURRENT_JOB.reset(token)
        task = asyncio.create_task(asyncio.wait_for(execute(), 120))
        self.jobs[ticket] = task
        if self.on_task:
            self.on_task(task)
        return ticket

    async def poll(self, ticket):
        task = self.jobs.get(ticket)
        if task is None:
            raise ValueError("Unknown operation")
        if not task.done():
            await asyncio.wait([task], timeout=0.05)
        events, self.events = self.events, []
        result = {"events": events, "pending": not task.done()}
        if task.done():
            try:
                result["result"] = {"ok": True, "value": task.result()}
            except BaseException as error:
                # Call logs may contain secrets; only typed, bounded messages escape.
                code, message, uncertain = "browser_proxy_error", "Browser operation failed; inspect the page before retrying.", True
                if isinstance(error, asyncio.CancelledError):
                    code, message = "browser_operation_cancelled", "Browser operation was cancelled; inspect the page in a fresh Python cell before repeating effects."
                elif isinstance(error, TimeoutError) or type(error).__name__ == "TimeoutError":
                    code, message = "browser_timeout", "Browser operation timed out; inspect the page before repeating effects."
                elif isinstance(error, AssertionError):
                    code, message, uncertain = "browser_assertion_failed", "Playwright assertion failed.", False
                elif isinstance(error, (ValueError, TypeError, AttributeError)):
                    code, message, uncertain = "browser_invalid_argument", "Invalid browser arguments; inspect the supported signature with workflow.describe().", False
                result["result"] = {"ok": False, "code": code, "error": message, "outcomeUncertain": uncertain}
        return result

    def reply(self, ticket, result):
        future = self.callbacks.get(ticket)
        if future is None or future.done():
            raise ValueError("Expired callback")
        if result.get("ok"):
            future.set_result(self.decode(result.get("value")))
        else:
            future.set_exception(RuntimeError("Python callback failed"))

    async def close(self):
        self.closed = True
        for target, event, _, wrapper in self.subscriptions:
            target.remove_listener(event, wrapper)
            if event == "dialog":
                target._proxy_dialog_listeners = max(0, getattr(target, "_proxy_dialog_listeners", 0) - 1)
        for future in self.callbacks.values():
            if not future.done():
                future.cancel()
        for job in self.jobs.values():
            if not job.done():
                job.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)
        for target, member, args, kwargs in self.routes:
            if member == "route":
                try:
                    await target.unroute(args[0] if args else kwargs["url"], args[1] if len(args) > 1 else kwargs.get("handler"))
                except Exception:
                    pass
        for target in self.dialog_expectations:
            target._proxy_dialog_listeners = max(0, getattr(target, "_proxy_dialog_listeners", 0) - 1)
        for target, locator in self.locator_handlers:
            try:
                await target.remove_locator_handler(locator)
            except Exception:
                pass
        for target, routes, routers in self.har_routes:
            try:
                for route in routes:
                    if route in target._impl_obj._routes:
                        target._impl_obj._routes.remove(route)
                await target._impl_obj._update_interception_patterns()
                for router in routers:
                    if router in target._impl_obj._har_routers:
                        target._impl_obj._har_routers.remove(router)
                        router.dispose()
            except Exception:
                pass
        self.har_routes.clear()
        for target, route in self.websocket_routes:
            try:
                target._impl_obj._web_socket_routes.remove(route)
                await target._impl_obj._update_web_socket_interception_patterns()
            except Exception:
                pass
        def expired(*args):
            raise RuntimeError("The Python callback's invocation has ended")
        for target, name in self.bindings:
            if target._proxy_bindings.get(name) == self.invocation:
                # Playwright 1.55 has no public unexpose method. Replace the binding
                # with a closure-free tombstone, then rebind it on a later cell.
                target._impl_obj._bindings[name] = expired
        for obj in self.objects.values():
            try:
                if self.class_name(obj) in {"JSHandle", "ElementHandle"}:
                    await obj.dispose()
                elif self.class_name(obj) == "Dialog":
                    await obj.dismiss()
                elif self.class_name(obj) == "Route":
                    await obj.abort()
                elif isinstance(obj, EventExpectation) and obj.info:
                    await obj.exit(cancel=True)
            except Exception:
                pass
        self.objects.clear()
        self.files.cleanup()
