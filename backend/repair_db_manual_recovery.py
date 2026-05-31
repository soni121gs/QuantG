import os
from pymongo import MongoClient

mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
client = MongoClient(mongo_url)
db = client['quantg']

print("=== REPAIRING MANUAL_RECOVERY MONGO RECORDS ===")

query = {"id": "manual_recovery"}
update = {
    "$set": {
        "description": "Manual position recovery override tool",
        "kind": "visual"
    }
}

res = db.strategies.update_many(query, update)
print(f"Update operation matched {res.matched_count} documents.")
print(f"Successfully repaired {res.modified_count} MANUAL_RECOVERY documents in MongoDB!")
