import sqlite3
import json
import sys

def mock_topology(gateway_id, mock_type):
    conn = sqlite3.connect('/data/config.db')
    cur = conn.cursor()
    cur.execute("SELECT profile_json FROM gateways WHERE short_id = ?", (gateway_id,))
    row = cur.fetchone()
    if not row:
        print(f"Gateway {gateway_id} not found.")
        sys.exit(1)
        
    profile = json.loads(row[0] or '{}')
    
    # Reset flags
    profile['has_ahub'] = False
    profile['has_apbox'] = False
    profile['remote_solar'] = False
    profile['apbox_remote_solar'] = None
    
    if mock_type == "ahub":
        profile['has_ahub'] = True
    elif mock_type == "apbox":
        profile['has_apbox'] = True
        profile['remote_solar'] = True
        profile['apbox_remote_solar'] = {"pv1": True, "pv2": True}
        
    cur.execute("UPDATE gateways SET profile_json = ? WHERE short_id = ?", (json.dumps(profile), gateway_id))
    
    # Also update the columns explicitly if needed
    if mock_type == "ahub":
        cur.execute("UPDATE gateways SET has_ahub = 1, has_apbox = 0 WHERE short_id = ?", (gateway_id,))
    elif mock_type == "apbox":
        cur.execute("UPDATE gateways SET has_apbox = 1, has_ahub = 0 WHERE short_id = ?", (gateway_id,))
        
    conn.commit()
    conn.close()
    print(f"Mocked {mock_type} for gateway {gateway_id}.")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python mock_topology.py <gateway_id> <ahub|apbox|reset>")
        sys.exit(1)
    mock_topology(sys.argv[1], sys.argv[2])
