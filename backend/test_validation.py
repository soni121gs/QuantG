import os
import sys
from pymongo import MongoClient

# Setup environment to load server modules
backend_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(backend_dir)

try:
    from server import StrategyOut
    from pydantic import ValidationError
    print("Successfully imported StrategyOut from server.py")
except Exception as e:
    print(f"Error importing StrategyOut: {e}")
    sys.exit(1)

# Connect to local MongoDB
client = MongoClient('mongodb://localhost:27017')
db = client['quantg']

user = db.users.find_one({"email": "soni121.gs@gmail.com"})
if not user:
    print("User soni121.gs@gmail.com not found!")
    sys.exit(1)

uid = user["id"]
print(f"Inspecting strategies for user: soni121.gs@gmail.com (id: {uid})")

strategies = list(db.strategies.find({"user_id": uid}))
print(f"Found {len(strategies)} strategies in MongoDB.")

passed = 0
failed = 0

for idx, s in enumerate(strategies):
    # Convert MongoDB dict (with ObjectId or _id etc) into a pure dict
    clean = dict(s)
    clean.pop("_id", None)
    clean.pop("user_id", None)
    
    # Try to validate using StrategyOut
    try:
        # Mimic the server.py _strategy_out conversion logic
        from server import _strategy_out
        obj = _strategy_out(s)
        passed += 1
        # print(f"  - Strat {idx+1} '{s.get('name')}' PASSED validation.")
    except ValidationError as ve:
        failed += 1
        print(f"\n[VALIDATION FAILED] Strat {idx+1}: '{s.get('name')}' (id: {s.get('id')})")
        print(f"ValidationError details:")
        print(ve)
    except Exception as e:
        failed += 1
        print(f"\n[OTHER ERROR] Strat {idx+1}: '{s.get('name')}' (id: {s.get('id')})")
        print(f"Error: {e}")

print(f"\n=== VALIDATION SUMMARY ===")
print(f"Total: {len(strategies)}")
print(f"Passed: {passed}")
print(f"Failed: {failed}")
