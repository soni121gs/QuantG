from pymongo import MongoClient
import json

client = MongoClient('mongodb://localhost:27017')
db = client['quantg']

print("=== MONGO ARCHITECTURE & RECORD CHECK ===")

# 1. Check all collections and counts
print("\n--- Collections and Document Counts ---")
for col in sorted(db.list_collection_names()):
    print(f"Collection '{col}': {db[col].count_documents({})} documents")

# 2. Check Users
print("\n--- Registered Users ---")
users = list(db.users.find({}, {"_id": 0}))
for u in users:
    print(f"User: id={u.get('id')} | email={u.get('email')} | paper_mode={u.get('paper_mode')}")

# 3. Check Strategies
print("\n--- Strategies in DB ---")
strategies = list(db.strategies.find({}, {"_id": 0}))
print(f"Total strategies: {len(strategies)}")
for idx, s in enumerate(strategies):
    print(f"Strat {idx+1}: id={s.get('id')} | name='{s.get('name')}' | user_id={s.get('user_id')} | status={s.get('status')} | mode={s.get('mode')} | broker={s.get('broker')}")

# 4. Check match/integrity
print("\n--- User/Strategy Mapping Check ---")
if not users:
    print("WARNING: No users found in database!")
else:
    for u in users:
        u_strats = list(db.strategies.find({"user_id": u.get("id")}))
        print(f"User {u.get('email')} (id={u.get('id')}) has {len(u_strats)} strategies.")
        for s in u_strats:
            print(f"  - '{s.get('name')}' (id={s.get('id')}) [status: {s.get('status')}]")
