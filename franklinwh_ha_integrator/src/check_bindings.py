import sqlite3

conn = sqlite3.connect('/data/config.db')
cur = conn.cursor()
cur.execute("DELETE FROM agate_utility_links WHERE gateway_short_id='99900001' AND utility_service_id != 'ede99f1f-0a9a-4b65-978f-78dd1f72e5d6'")
conn.commit()
print(f"Deleted {cur.rowcount} duplicate bindings.")
conn.close()
