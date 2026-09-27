import json
from src.services.gateway_service import GatewayService

svc = GatewayService("123", "FULL123", {})

# Mock a results dict with smart circuits V3 format
results = {
    "smart_circuits": {
        "1": {
            "name": "Circuit 1",
            "mode": 0,
            "is_on": False,
            "soc_cutoff_enabled": False,
            "soc_cutoff_limit": 0,
            "pro_load_type": 0,
            "time_enabled": [0,0,0,0],
            "time_schedules": ["2025-10-04 20:11", "2025-10-04 20:12", "2025-10-04 00:00", "2025-10-04 23:59"],
            "time_set": [1,0,1,0]
        },
        "2": {
            "name": "Circuit Test",
            "mode": 1,
            "is_on": True,
            "soc_cutoff_enabled": False,
            "soc_cutoff_limit": 0,
            "pro_load_type": 1,
            "time_enabled": [0,0,0,0],
            "time_schedules": ["2025-10-17 18:07", "2025-10-17 18:08", "2025-10-17 00:00", "2025-10-17 23:59"],
            "time_set": [1,0,1,0]
        }
    }
}

d = {"switch_1_load": 120}

# Actually we pass the whole results to `_normalise_stats`
class DummyStats:
    pass

stats = DummyStats()
stats.current = {"switch_1_load": 400}
results["stats"] = {"current": {"switch_1_load": 400}}

try:
    normalized = svc._normalise_stats(results)
    for i in range(1, 4):
        print(f"Circuit {i} Name:", normalized.get(f"smart_circuit_{i}_name"))
        print(f"Circuit {i} State:", normalized.get(f"smart_circuit_{i}_state"))
        print(f"Circuit {i} Power:", normalized.get(f"smart_circuit_{i}_power"))
except Exception as e:
    import traceback
    traceback.print_exc()
