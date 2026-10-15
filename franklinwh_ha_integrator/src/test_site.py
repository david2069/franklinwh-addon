import asyncio
import json
import configparser
import sys
from franklinwh_cloud.client import TokenFetcher, Client

async def main():
    config = configparser.ConfigParser()
    # Path inside the container is /app/franklinwh.ini but actually franklinwh.ini isn't mounted!
    # Let me just hardcode the env variables if config fails.
    import os
    email = os.environ.get("CLOUD_EMAIL", "")
    pwd = os.environ.get("CLOUD_PASSWORD", "")

    if not email:
        print("No CLOUD_EMAIL found")
        sys.exit(1)

    fetcher = TokenFetcher(email, pwd)
    await fetcher.get_token()
    client = Client(fetcher, gateway="none")
    res = await client.get_site_and_device_info()
    print(json.dumps(res, indent=2))

asyncio.run(main())
