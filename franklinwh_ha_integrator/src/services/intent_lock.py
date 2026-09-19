import time
import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)

class DispatchIntent:
    def __init__(self, action: str, rule_id: str, priority: int, duration_mins: int = 5, payload: dict = None):
        self.action = action
        self.rule_id = rule_id
        self.priority = priority
        self.duration_secs = duration_mins * 60
        self.payload = payload or {}
        self.start_time = time.time()
        self.expires_at = self.start_time + self.duration_secs

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    def __repr__(self):
        return f"<DispatchIntent action={self.action} rule={self.rule_id} priority={self.priority} remaining={int(self.expires_at - time.time())}s>"

class IntentManager:
    """
    Manages active dispatch intents (locks) to prevent rule flapping.
    A new intent can only override an existing one if:
    1. The existing one is expired.
    2. The new one has a HIGHER priority (lower number).
    """
    def __init__(self):
        self._intents: Dict[str, DispatchIntent] = {}  # gateway_serial -> intent

    def get_active_intent(self, gateway_serial: str) -> Optional[DispatchIntent]:
        intent = self._intents.get(gateway_serial)
        if intent and intent.is_expired:
            logger.debug(f"IntentManager: intent for {gateway_serial} expired ({intent.action})")
            del self._intents[gateway_serial]
            return None
        return intent

    def request_intent(self, gateway_serial: str, action: str, rule_id: str, priority: int, duration_mins: int = 5, payload: dict = None) -> bool:
        """
        Request a new intent. Returns True if granted (overrides or new).
        """
        existing = self.get_active_intent(gateway_serial)
        
        if not existing:
            self._intents[gateway_serial] = DispatchIntent(action, rule_id, priority, duration_mins, payload)
            logger.info(f"IntentManager: new intent granted for {gateway_serial}: {action} (rule={rule_id}, dur={duration_mins}m)")
            return True

        if priority < existing.priority:
            logger.info(f"IntentManager: priority override for {gateway_serial}: {action} ({priority}) overrides {existing.action} ({existing.priority})")
            self._intents[gateway_serial] = DispatchIntent(action, rule_id, priority, duration_mins, payload)
            return True

        if action == existing.action:
            # Refresh duration if same action
            existing.expires_at = time.time() + (duration_mins * 60)
            return True

        logger.debug(f"IntentManager: intent denied for {gateway_serial}: {action} ({priority}) blocked by {existing.action} ({existing.priority})")
        return False

    def clear_intent(self, gateway_serial: str):
        if gateway_serial in self._intents:
            del self._intents[gateway_serial]

# Global singleton
manager = IntentManager()
