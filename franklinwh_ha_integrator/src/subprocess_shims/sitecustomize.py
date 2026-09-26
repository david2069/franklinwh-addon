"""sitecustomize.py — injected into subprocesses via PYTHONPATH.

Python's `site` module auto-imports `sitecustomize` at interpreter
startup if it's importable. When FHAI spawns the franklinwh-cli
subprocess (or any other franklinwh_cloud CLI), Python evaluates
`discovery.py`'s module-level return annotation `-> ResolvedCapabilities:`
at import time — but that class is only imported inside function scope
in the current upstream (unreleased/patched) `franklinwh_cloud` package.
Result: NameError before the CLI can even parse its args.

FHAI's main app process works around this in `src/__init__.py` by
injecting a dummy `ResolvedCapabilities` into `builtins`, but that
shim is scoped to the main Python process and does NOT survive across
subprocess.exec boundaries. Every subprocess needs its own shim.

Setting `PYTHONPATH=/app/src/subprocess_shims:...` in the subprocess
env is enough — Python's site machinery finds this file on the path
and imports it before user code, and this class ends up in `builtins`
before `franklinwh_cloud.discovery` evaluates its annotation.

Related: `src/routes/api_terminal.py` (the site that sets PYTHONPATH),
`src/__init__.py` (equivalent shim for the main app process).
"""
import builtins


class DummyResolvedCapabilities:
    pass


builtins.ResolvedCapabilities = DummyResolvedCapabilities
