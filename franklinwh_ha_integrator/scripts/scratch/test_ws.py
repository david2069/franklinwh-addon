import asyncio
import websockets
import json

async def test():
    try:
        async with websockets.connect("ws://localhost:8099/api/ws/mqtt") as ws:
            print("Connected!")
            await ws.send(json.dumps({"action": "subscribe", "topic": "#"}))
            # Test proxy streaming
            count = 0
            while count < 3:
                msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                print(f"Received: {msg[:100]}")
                count += 1
    except Exception as e:
        print(f"Failed: {e}")

asyncio.run(test())
