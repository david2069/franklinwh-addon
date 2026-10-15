import logging
import httpx
import json
from typing import Any, Dict, Optional
from src.services.notification_sender import _get_ha_credentials

logger = logging.getLogger(__name__)

class HomeAssistantBridge:
    """
    Bridge for executing Home Assistant service calls from the Smart Dispatch engine.
    """
    def __init__(self):
        self._ha_url = None
        self._ha_token = None

    async def _ensure_creds(self):
        if not self._ha_url or not self._ha_token:
            self._ha_url, self._ha_token = await _get_ha_credentials()

    async def call_service(self, service_path: str, params: Any = None) -> bool:
        """
        Call an HA service.
        service_path: e.g. "switch.turn_on" or "climate.set_temperature"
        params: dict of service data
        """
        await self._ensure_creds()
        if not self._ha_url or not self._ha_token:
            logger.error("HABridge: No HA credentials configured. Cannot call service.")
            return False

        if not service_path or "." not in service_path:
            logger.error(f"HABridge: Invalid service path: {service_path}")
            return False

        domain, service = service_path.split(".", 1)
        url = f"{self._ha_url}/api/services/{domain}/{service}"
        
        headers = {
            "Authorization": f"Bearer {self._ha_token}",
            "Content-Type": "application/json",
        }
        
        # Parse params if string
        data = params
        if isinstance(params, str):
            try:
                data = json.loads(params)
            except Exception:
                data = {}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=headers, json=data or {})
                if resp.status_code in (200, 201):
                    logger.info(f"HABridge: Successfully called {service_path} with {data}")
                    return True
                else:
                    logger.error(f"HABridge: Failed to call {service_path}: {resp.status_code} {resp.text}")
                    return False
        except Exception as e:
            logger.error(f"HABridge: Exception calling HA service: {e}")
            return False

# Global singleton
manager = HomeAssistantBridge()
