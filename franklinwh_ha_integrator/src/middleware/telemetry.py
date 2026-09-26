"""Counts API calls into local telemetry counters. Never transmits.

Deliberately records the **route template** rather than the request path.
`/api/gateways/{short_id}` is a metric; `/api/gateways/99900000000099900001` is
a gateway serial embedded in a counter name, which would put an identifier into
the very payload the allowlist exists to keep clean. Concrete paths also
explode cardinality — one counter per gateway, per session, per id.

Requests that matched no route are skipped: a 404 path is attacker- or
typo-controlled free text and has no business becoming a metric name.
"""
import logging

from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


def _templatise(request) -> str:
    """Full request path with path parameters replaced by their names.

    Built from the request path rather than `route.path`, which omits the
    router's mount prefix: a request to `/api/gateways/status/all` matched a
    route reporting `/gateways/status/all`, so exclusion prefixes written with
    `/api` silently matched nothing and the busiest poller in the app was
    counted as usage. Caught only by running it — the unit tests exercised
    is_excluded() directly and never saw the derivation.

    Substitution is per path segment, never a substring replace: a gateway id
    that happened to appear inside another segment would otherwise corrupt it.
    """
    params = {str(v): k for k, v in (request.path_params or {}).items()}
    segments = request.url.path.split("/")
    return "/".join("{" + params[seg] + "}" if seg in params else seg
                    for seg in segments)


class TelemetryMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)

        try:
            await self._count(request, response)
        except Exception as exc:
            # Telemetry must never affect the response. A counter is worth
            # nothing next to a working request.
            logger.debug(f"telemetry middleware: skipped ({exc!r})")

        return response

    @staticmethod
    async def _count(request, response) -> None:
        from src.services import telemetry

        # A matched route is required: an unmatched (404) path is typo- or
        # attacker-controlled free text and has no business becoming a metric.
        if request.scope.get("route") is None:
            return

        template = _templatise(request)
        if telemetry.is_excluded(template):
            return

        method = request.method.upper()
        if method == "OPTIONS":
            return

        # Bucket by outcome class, not exact status: "did this work" is the
        # useful signal and exact codes add cardinality without insight.
        outcome = "ok" if response.status_code < 400 else "err"
        await telemetry.record(f"api.{method}.{template}.{outcome}")
