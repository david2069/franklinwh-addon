import asyncio
from src.services import db

async def go():
    db.set_db_path("data/franklinwh.db")
    import os
    if not os.path.exists("data/franklinwh.db"):
        db.set_db_path("data/data.db")
    gws = await db.get_all_gateways()
    if not gws:
        print("No gateways")
        return
        
    creds = await db.get_all_credentials()
    c = creds[0]
    
    from franklinwh_cloud import FranklinWHCloud
    client = FranklinWHCloud(c["email"], c["password"])
    await client.login()
    await client.select_gateway(gws[0]["full_serial"])
    
    import json
    url = client.url_base + "hes-gateway/terminal/newCompliance/getComplianceDetailById"
    try:
        res = await client._get(url, params={"gatewayId": client.gateway, "systemId": 0})
        print("SYSTEM 0:", json.dumps(res, indent=2))
    except Exception as e: print("SYSTEM 0 ERR:", e)
    
    try:
        res = await client._get(url, params={"gatewayId": client.gateway, "systemId": 28})
        print("SYSTEM 28:", json.dumps(res, indent=2))
    except Exception as e: print("SYSTEM 28 ERR:", e)

if __name__ == "__main__":
    asyncio.run(go())
