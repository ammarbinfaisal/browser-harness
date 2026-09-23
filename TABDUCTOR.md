# Tabductor Camoufox integration

Based on upstream `afbcc381b963040c19627d788e40c7e7663171ee`.
Tabductor uses these entry points, independently of the upstream CDP CLI:

* `tabductor_runner.py`: generated Python in a disposable container, with a JSON
  stdin/stdout gateway. No browser context, worker credentials, network, or host
  filesystem is available in this container.
* `camoufox.py`: trusted primitives injected with the leased Playwright page from
  Tabductor's Camoufox worker. It never starts a browser or attaches through CDP.
* `Dockerfile.tabductor`: isolated runner image (Python standard library only).

Each primitive goes through Tabductor's host dispatcher before touching the
browser. The host records arguments, read results, write acknowledgements,
failures, helper revisions, business events and completion. It owns policy,
input fencing, journaling and verification; Python stdout cannot claim a task
completed. Normal Python output goes to stderr and returns to the agent.

The `api`/`td` namespace accepts keyword arguments or a dictionary, unwraps
successful host results, and raises on errors. `api.input` is the trigger packet.
Familiar helpers such as `goto_url`, `observe`, `find`, `click`, `fill_input`,
`js`, `scroll`, and `capture_screenshot` call the same dispatcher. This is an
adapter for Tabductor, not a drop-in CDP implementation of every upstream helper.

Use `api.helpers.define(name='helper_name', source='def helper_name(...): ...')`
to save Python functions. They become available on the next invocation.
Editing `agent_helpers.py` saves a task-scoped module when the invocation returns
normally; return before calling `api.run.done()` if a file edit needs saving.
Helper definitions must not perform browser work at module initialization.
Invocations have fresh globals and files; only saved helper revisions and
host checkpoints persist. Python and JavaScript helper histories are separate.

Tabductor compiles the recorded operations into JavaScript; it does not transpile
Python source. Raw page evaluation, coordinate actions, unbound typing and file
effects remain AI-only until their replay semantics can be validated.

Run the Tabductor acceptance suite from its parent checkout with
`pnpm test:python-harness`. It builds disposable Camoufox, broker and runner
containers, tests compilation/recovery and isolation, and cleans up afterward.
