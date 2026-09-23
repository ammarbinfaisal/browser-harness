# Tabductor Camoufox SDK

Based on upstream `afbcc381b963040c19627d788e40c7e7663171ee`.

`sdk.py` provides importable browser-harness functions with an injected transport
and recorder. `tabductor_runner.py --supervise` starts a fresh Python interpreter
per cell inside one run-scoped sandbox. `camoufox.py` implements trusted browser
primitives against an already leased Playwright page. No entry point starts
Chrome, attaches through CDP, or discovers another run's tabs.

Import SDK calls from `browser_harness`, or use the runner's pre-imported names.
Browser JS uses the main world when explicitly requested through the host.
Authenticated requests use the browser, with explicit read/write semantics.
The embedding host parses top-level return/await before evaluation and implements
native clipboard paste under its browser input lock.

The runner's `api` / `td` bridge accepts keyword arguments or a dict, unwraps host
results, and raises on failure. `api.input` contains the trigger packet. Standard
library imports and ordinary workspace files are supported. Files checkpoint at
cell exit (also on exceptions and terminal calls); `api.checkpoint_files()` saves
explicitly. Python globals reset each cell. The host restores committed files
when a sandbox is replaced, preserving the effect journal without replaying code.

Every public SDK call records arguments, results/errors, duration and nesting.
Host operations carry the corresponding SDK call ID and helper provenance. The
host owns leases, validation and effect receipts; guest trace events cannot claim
business completion. Protocol v2 is required by the host.

Define helpers through `api.helpers.define`, or edit `agent_helpers.py`. Valid
edits persist even when the cell raises or calls `api.run.done()`. Module
initialization cannot execute browser/workflow operations.

Recorded Python data flow compiles to guarded JavaScript over the same browser
and workspace capabilities. A deopt hands the run to an AI using Python SDK calls,
with the same session and files. Recompilation happens after recovery finishes.

See the parent checkout's `docs/python-browser-harness.md` for API details,
limits, deployment and acceptance commands. Upstream's CDP CLI remains separate;
this SDK does not emulate arbitrary CDP methods.
