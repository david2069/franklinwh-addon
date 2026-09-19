import asyncio
import json
from src.services import db
from src.main import get_app_state

async def test():
    db.set_db_path("data/franklinwh.db")
    import os
    if not os.path.exists("data/franklinwh.db"):
        db.set_db_path("data/data.db")
    gws = await db.get_all_gateways()
    if not gws:
        print("No gateways")
        return
    short_id = gws[0]["short_id"]
    from franklinwh_cloud import FranklinWHCloud
    creds = await db.get_all_credentials()
    c = creds[0]
    client = FranklinWHCloud(c["email"], c["password"])
    await client.login()
    await client.select_gateway(gws[0]["full_serial"])
    
    print("--- REQ 1 ---")
    try:
        res1 = await client.get_grid_profile_info(requestType=1)
        print(json.dumps(res1, indent=2))
    except Exception as e:
        print("REQ1 Error:", e)
        
    print("--- REQ 2 ---")
    try:
        res2 = await client.get_grid_profile_info(requestType=2)
        print(json.dumps(res2, indent=2))
    except Exception as e:
        print("REQ2 Error:", e)

asyncio.run(test())
