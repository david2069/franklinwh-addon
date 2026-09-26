import asyncio
import json
from src.services import db
from src.main import get_app_state

async def test():
    db.set_db_path("data/franklinwh.db")
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
    res = await client.get_grid_profile_info(requestType=2)
    print(json.dumps(res, indent=2))

asyncio.run(test())
