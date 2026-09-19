import asyncio
import logging
from datetime import datetime
from collections import deque

logger = logging.getLogger("bms_history")

# History Buffer
# format: { "gw_id" : { "battery_serial": { "times": deque, "voltages": deque, "temps": deque } } }
HISTORY_MAX_LEN = 100

class BMSHistoryManager:
    def __init__(self):
        self.history = {}
        self._running = False
        self._task = None

    async def _poll_loop(self):
        logger.info("BMS History RAM scraper activated. Archiving cache arrays every 120s.")
        while self._running:
            try:
                from src.main import get_app_state
                registry = get_app_state().get("registry")
                if not registry:
                    await asyncio.sleep(10)
                    continue

                for gw in registry._services.values():
                    if not gw._client: continue
                    
                    bms_units = gw.status.last_data.get("bms_units", [])
                    if not bms_units: continue
                    
                    if gw.short_id not in self.history:
                        self.history[gw.short_id] = {}
                        
                    for unit in bms_units:
                        serial = unit.get("serial")
                        if not serial: continue
                        
                        if serial not in self.history[gw.short_id]:
                            self.history[gw.short_id][serial] = {
                                "times": deque(maxlen=HISTORY_MAX_LEN),
                                "voltages": deque(maxlen=HISTORY_MAX_LEN),
                                "temps": deque(maxlen=HISTORY_MAX_LEN)
                            }
                            
                        # Append snapshot
                        if "cell_voltages" in unit and "cell_temps" in unit:
                            h = self.history[gw.short_id][serial]
                            
                            # Only append if the last snapshot isn't identical (to avoid stalling out duplicate polls)
                            if len(h["times"]) > 0:
                                last_v = h["voltages"][-1]
                                if last_v == unit["cell_voltages"]:
                                    continue
                                    
                            h["times"].append(datetime.now().strftime("%H:%M:%S"))
                            h["voltages"].append(unit["cell_voltages"])
                            h["temps"].append(unit["cell_temps"])
            except Exception as e:
                logger.error(f"Error in BMS history poller: {e}")
                
            await asyncio.sleep(120)

    def start(self):
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._poll_loop())

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    def get_history(self, gw_short_id: str):
        if gw_short_id not in self.history:
            return {}
        
        # Convert deques to lists for JSON serialization
        res = {}
        for serial, h in self.history[gw_short_id].items():
            res[serial] = {
                "times": list(h["times"]),
                "voltages": list(h["voltages"]),
                "temps": list(h["temps"])
            }
        return res

bms_history_manager = BMSHistoryManager()
