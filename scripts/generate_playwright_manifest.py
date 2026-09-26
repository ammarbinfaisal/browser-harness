"""Generate the proxy contract from the worker's pinned Playwright installation.

Run in the browser-worker image; stdout is the deterministic manifest.
"""
import inspect
import json
import typing
from importlib.metadata import version
from playwright import sync_api
from playwright.sync_api import _generated

# Cover every generated class, including returned types not re-exported by sync_api
# (Clock and Tracing). Ownership/lifecycle policy belongs to the worker, not the API.
CLASSES = {name: value for name, value in vars(_generated).items()
           if inspect.isclass(value) and value.__module__ == _generated.__name__}
READS = set("url title content inner_text inner_html text_content input_value all_text_contents all_inner_texts get_attribute count bounding_box is_visible is_hidden is_enabled is_disabled is_checked is_editable is_closed is_detached is_intersecting_viewport all_headers header_value header_values headers_array body text json finished failure sizes server_addr security_details json_value get_property get_properties aria_snapshot screenshot wait_for wait_for_selector wait_for_function wait_for_load_state wait_for_url wait_for_event wait_for_timeout".split())
CONSTRUCTORS = set("locator get_by_role get_by_text get_by_label get_by_placeholder get_by_alt_text get_by_title get_by_test_id frame frame_locator filter nth and_ or_ all element_handle element_handles query_selector query_selector_all expect_event expect_popup expect_page expect_request expect_response expect_request_finished expect_download expect_file_chooser expect_console_message expect_websocket".split())

def generate():
    assert version("playwright") == "1.55.0", "Regenerate deliberately when upgrading Playwright"
    classes, metadata = {}, {}
    for name, cls in CLASSES.items():
        members = {}
        for member, value in inspect.getmembers(cls):
            if member.startswith("_"):
                continue
            prop = isinstance(value, property)
            fn = value.fget if prop else value
            if not callable(fn):
                continue
            params = []
            for p in inspect.signature(fn).parameters.values():
                if p.name == "self":
                    continue
                params.append({"name": p.name, "kind": p.kind.name,
                               "required": p.default is inspect.Parameter.empty,
                               **({"default": p.default} if p.default is not inspect.Parameter.empty else {}),
                               "annotation": inspect.formatannotation(p.annotation) if p.annotation is not inspect.Parameter.empty else None})
            kind = "assertion" if name.endswith("Assertions") else "read" if prop or member in READS or member in CONSTRUCTORS else "effect"
            if member in {"on", "once", "remove_listener", "route", "unroute", "unroute_all", "route_web_socket"}:
                kind = "subscription"
            members[member] = {"property": prop, "parameters": params, "kind": kind,
                               "signature": str(inspect.signature(fn))}
        classes[name] = members
        metadata[name] = {"bases": [base.__name__ for base in cls.__bases__ if base.__name__ in CLASSES],
                          "contextManager": any(base.__name__ == "SyncContextManager" for base in cls.__mro__)}
    structures = {name: {"fields": {field: inspect.formatannotation(annotation)
                                   for field, annotation in getattr(sync_api, name).__annotations__.items()},
                         "total": getattr(sync_api, name).__total__}
                  for name in sync_api.__all__ if typing.is_typeddict(getattr(sync_api, name))}
    return {"version": "playwright-python-v1", "playwright": version("playwright"),
            "classes": classes, "metadata": metadata, "structures": structures,
            "aliases": {name: value.__name__ for name in sync_api.__all__
                        if inspect.isclass(value := getattr(sync_api, name))
                        and value.__module__ == _generated.__name__ and name != value.__name__}}

if __name__ == "__main__":
    print(json.dumps(generate(), indent=2, sort_keys=True))
