"""FranklinWH HA Integrator"""
__version__ = "0.6.54"

# Workaround for NameError in unreleased/patched versions of franklinwh-cloud.
# Python 3.12 evaluates return type annotations at module import time, causing
# a NameError in discovery.py because ResolvedCapabilities is only imported
# inside the function scope. We inject a dummy class into builtins during startup.
# The actual function imports the real class from models within its local scope.
import builtins
class DummyResolvedCapabilities:
    pass
builtins.ResolvedCapabilities = DummyResolvedCapabilities


