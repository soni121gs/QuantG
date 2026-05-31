import os
import sys
import asyncio
from pymongo import MongoClient

# Setup environment to load server modules
backend_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(backend_dir)

# Connect to local MongoDB
client = MongoClient('mongodb://localhost:27017')
db = client['quantg']

user = db.users.find_one({"email": "soni121.gs@gmail.com"})
if not user:
    print("User soni121.gs@gmail.com not found!")
    sys.exit(1)

uid = user["id"]

async def test_endpoint_directly():
    print(f"=== TESTING list_strategies ENDPOINT FUNCTION IN-MEMORY ===")
    
    try:
        from server import list_strategies
        print("Successfully imported list_strategies from server.py")
    except Exception as e:
        print(f"Error importing list_strategies: {e}")
        return False
        
    # Mock user object as returned by get_current_user dependency
    mock_user = {
        "id": uid,
        "email": "soni121.gs@gmail.com",
        "role": "trader",
        "approved": True
    }
    
    try:
        # Call the fastapi route function directly
        result = await list_strategies(user=mock_user)
        print(f"Endpoint function returned successfully with HTTP 200 equivalent!")
        print(f"Number of strategies returned: {len(result)}")
        
        # Verify StrategyOut Pydantic model serialization is correct
        for idx, strat_out in enumerate(result):
            print(f"  {idx+1}. name='{strat_out.name}' | status={strat_out.status} | kind={strat_out.kind} | mode={strat_out.mode}")
            
        print("\nAPI ENDPOINT VERIFICATION: SUCCESS / PASS")
        return True
    except Exception as e:
        print(f"\nAPI ENDPOINT VERIFICATION: FAILED / FAIL")
        print(f"Exception during endpoint execution: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    asyncio.run(test_endpoint_directly())
