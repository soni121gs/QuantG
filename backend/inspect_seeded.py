from pymongo import MongoClient
import json

client = MongoClient('mongodb://localhost:27017')
db = client['quantg']

print("=== DEEP STRATEGY SEEDING INSPECTION ===")

# 1. Users list
print("\n--- Users in MongoDB ---")
users = list(db.users.find({}, {"_id": 0, "password_hash": 0}))
for u in users:
    print(f"User: email={u.get('email')} | id={u.get('id')} | role={u.get('role')} | approved={u.get('approved')}")

# 2. Count strategies per user
print("\n--- Strategies per User Count ---")
for u in users:
    count = db.strategies.count_documents({"user_id": u.get("id")})
    print(f"User '{u.get('email')}' (id={u.get('id')}) has {count} strategies in DB.")

# 3. Check strategies with NULL or missing user_id
null_count = db.strategies.count_documents({"user_id": {"$in": [None, ""]}})
print(f"Strategies with null/empty user_id: {null_count}")

# 4. Check for any strategies that do not belong to any registered user ID
registered_user_ids = {u.get("id") for u in users}
all_strategies = list(db.strategies.find({}, {"_id": 0, "id": 1, "name": 1, "user_id": 1, "status": 1, "mode": 1}))
orphan_count = 0
for s in all_strategies:
    uid = s.get("user_id")
    if uid not in registered_user_ids:
        orphan_count += 1
        print(f"Orphan strategy: name='{s.get('name')}' | id={s.get('id')} | user_id={uid}")
print(f"Total orphan strategies: {orphan_count}")

# 5. Let's inspect the strategies for soni121.gs@gmail.com specifically
soni_user = db.users.find_one({"email": "soni121.gs@gmail.com"})
if soni_user:
    uid = soni_user["id"]
    soni_strats = list(db.strategies.find({"user_id": uid}))
    print(f"\n--- soni121.gs@gmail.com strategies details (Count: {len(soni_strats)}) ---")
    for idx, s in enumerate(soni_strats):
        print(f"{idx+1}. name='{s.get('name')}' | id={s.get('id')} | status={s.get('status')} | mode={s.get('mode')} | broker={s.get('broker')}")
else:
    print("\nWARNING: soni121.gs@gmail.com user not found in DB!")
