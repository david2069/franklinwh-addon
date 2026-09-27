import asyncio
import sqlite3
from franklinwh_cloud import Client


async def main():
    conn = sqlite3.connect('/data/config.db')
    row = conn.execute("SELECT email, password, serial FROM gateway_credentials").fetchone()
    email = row[0]
    password = row[1]
    
    client = Client(email, password)
    gateways = await client.get_gateways()
    gw_id = gateways[0]["id"]
    client.set_gateway(gw_id)
    
    print("Fetching BMS...")
    bms = await client.get_bms_info()
    import json
    print(json.dumps(bms, indent=2))

asyncio.run(main())
