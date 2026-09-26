"""
CommandListener — subscribes to HA MQTT command topics and routes to GatewayService.

Subscribes to: franklinwh/+/control/+/set
Routes parsed commands (short_id, slug, value) to the gateway registry.

Phase 3: basic routing scaffold — full Cloud API dispatch in Phase 6 (Controls tab).
"""
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

RECONNECT_BASE = 5
RECONNECT_MAX = 60


class CommandListener:
    """
    Subscribes to HA command topics and dispatches control actions.

    :param host:     MQTT broker hostname
    :param port:     MQTT broker port
    :param username: Optional broker username
    :param password: Optional broker password
    :param topic_prefix: State topic prefix (default "franklinwh")
    :param registry: GatewayRegistry instance — injected for dispatching
    """

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: str = "",
        password: str = "",
        client_id: Optional[str] = None,
        topic_prefix: str = "franklinwh",
        registry=None,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.client_id = client_id
        self.topic_prefix = topic_prefix
        self.registry = registry

        self._task: Optional[asyncio.Task] = None

    @property
    def subscribe_pattern(self) -> str:
        return f"{self.topic_prefix}/+/control/+/set"

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop(), name="mqtt-command-listener")
        logger.info(f"CommandListener started → {self.host}:{self.port}")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("CommandListener stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        import aiomqtt
        backoff = RECONNECT_BASE
        while True:
            try:
                client_kwargs = dict(hostname=self.host, port=self.port)
                if self.username:
                    client_kwargs["username"] = self.username
                if self.password:
                    client_kwargs["password"] = self.password
                if self.client_id:
                    client_kwargs["identifier"] = self.client_id

                async with aiomqtt.Client(**client_kwargs) as client:
                    backoff = RECONNECT_BASE
                    await client.subscribe(self.subscribe_pattern)
                    await client.subscribe(f"{self.topic_prefix}/loads/+/enabled/set")
                    logger.info(f"CommandListener subscribed to: {self.subscribe_pattern} and loads toggle")

                    async for message in client.messages:
                        await self._dispatch(str(message.topic), message.payload.decode())

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The publisher reports the same rejection with the same advice;
                # two identical ERRORs a minute apart read as two faults, so the
                # listener stays quiet about credentials and lets the publisher
                # own that message.
                from src.services import mqtt_errors

                if mqtt_errors.is_auth_failure(exc):
                    logger.debug(f"CommandListener: broker refused credentials: {exc}")
                else:
                    logger.warning(f"CommandListener disconnected: {exc}. Retrying in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)

    async def _dispatch(self, topic: str, payload: str) -> None:
        """
        Parse topic and route command to the appropriate control handler.

        Topic formats: 
          - franklinwh/{short_id}/control/{slug}/set
          - franklinwh/loads/{load_id}/enabled/set
        """
        parts = topic.split("/")
        if len(parts) >= 5 and parts[1] == "loads" and parts[3] == "enabled" and parts[4] == "set":
            load_id = parts[2]
            logger.info(f"[Loads] Enabled command received for load {load_id}: {payload}")
            from src.services.db import get_forecast_load, upsert_forecast_load
            load = await get_forecast_load(load_id)
            if load:
                val = 1 if payload.upper() in ("ON", "1", "TRUE") else 0
                load["enabled"] = val
                await upsert_forecast_load(load)
                logger.info(f"[Loads] Load {load_id} enabled status updated to {val} in database")
                
                # Instantly republish the state so HA updates
                from src.main import get_app_state
                publisher = get_app_state().get("publisher")
                if publisher:
                    publisher.enqueue(f"{self.topic_prefix}/loads/{load_id}/enabled", "ON" if val else "OFF", qos=0, retain=True)
            return

        if len(parts) < 5:
            logger.warning(f"Unexpected command topic: {topic}")
            return

        prefix, short_id, _, slug, action = parts[0], parts[1], parts[2], parts[3], parts[4]

        if action != "set":
            return

        logger.info(f"[{short_id}] Command received: {slug} = {payload!r}")

        # TODO Phase 6: Route to Cloud API control methods
        # e.g. operating_mode → client.set_mode(...)
        #      storm_hedge → client.set_storm_settings(...)
        #      battery_backup_reserve_soc → client.update_soc(...)
        await self._handle_command(short_id, slug, payload)

    def _echo(self, short_id: str, slug: str, value) -> None:
        """Confirm an entity's state on its state topic. Never raises.

        These entities are not optimistic, so Home Assistant keeps showing the
        previous value until the state topic says otherwise.
        """
        if value is None:
            return
        try:
            from src.main import get_app_state

            publisher = get_app_state().get("publisher")
            if publisher and not publisher.publish_entity_state(short_id, slug, value):
                logger.debug(f"[{short_id}] no state echo for {slug}")
        except Exception:
            logger.debug(f"[{short_id}] could not echo {slug} state", exc_info=True)

    async def _handle_command(self, short_id: str, slug: str, payload: str) -> None:
        """Route command to GatewayService via registry."""
        if not self.registry:
            logger.warning(f"[{short_id}] No registry attached — command {slug}={payload!r} dropped")
            return
        # The Control Lock is handled here, not on the gateway — it exists to
        # decide whether a command reaches the gateway at all.
        from src.services import control_lock

        on = str(payload).strip().upper() in ("ON", "TRUE", "1")

        if slug == control_lock.MASTER_SLUG:
            control_lock.set_master(on)
            self._echo(short_id, slug, "ON" if control_lock.is_enforcing() else "OFF")
            return

        for category, lock_slug in control_lock.SLUGS.items():
            if slug == lock_slug:
                control_lock.set_category(category, on)
                self._echo(short_id, slug, "ON" if control_lock.is_locked(category) else "OFF")
                return

        refusal = control_lock.check(slug, payload)
        if refusal:
            logger.warning(f"[{short_id}] REFUSED {slug}={payload!r}: {refusal}")
            # Put the entity back where it was, so the UI does not imply the
            # command was accepted.
            self._echo(short_id, slug, "On-Grid" if slug == "off_grid_mode" else None)
            return

        result = await self.registry.dispatch_command(short_id, slug, payload, source="ha_automation")
        if result.get("ok"):
            logger.info(f"[{short_id}] Command {slug}={payload!r} dispatched OK")
            # Confirm it on the state topic immediately. These entities are not
            # optimistic, so Home Assistant keeps showing the old value until
            # the state topic says otherwise — and that only happened on the
            # next poll, which is dropped whenever the cloud returns stale data.
            # The visible symptom is a dropdown snapping back after a change
            # that actually worked.
            self._echo(short_id, slug, result.get("value", payload))
        else:
            logger.error(f"[{short_id}] Command {slug} failed: {result.get('error')}")
