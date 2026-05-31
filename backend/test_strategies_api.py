import os
import sys
import jwt
import json
import urllib.request
import urllib.error
import hashlib
from datetime import datetime, timezone, timedelta
from pymongo import MongoClient

# Setup environment
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# MongoDB
client = MongoClient('mongodb://localhost:27017')
db = client['quantg']

# Find soni121.gs@gmail.com
user = db.users.find_one({"email": "soni121.gs@gmail.com"})
if not user:
    print("Error: User soni121.gs@gmail.com not found in MongoDB!")
    sys.exit(1)

user_id = user["id"]
email = user["email"]

# JWT setup from server.py logic
JWT_SECRET = os.environ.get("JWT_SECRET") or os.environ.get("SECRET_KEY") or "quantg-development-secret"
if len(JWT_SECRET.encode()) < 32:
    JWT_SECRET = hashlib.sha256(JWT_SECRET.encode()).hexdigest()
JWT_ALG = "HS256"

# Create token
payload = {
    "sub": user_id,
    "email": email,
    "exp": datetime.now(timezone.utc) + timedelta(minutes=60),
}
token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)

print(f"Generated JWT Token for {email} (id: {user_id}):")
print(f"Bearer {token[:20]}...{token[-20:]}\n")

# Hit the local FastAPI strategies endpoint
url = "http://localhost:8000/api/strategies"
headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json"
}

print(f"Sending GET request to {url}...")
req = urllib.request.Request(url, headers=headers)
try:
    with urllib.request.urlopen(req, timeout=10.0) as response:
        status_code = response.getcode()
        body = response.read().decode('utf-8')
        print(f"Response Status: {status_code}")
        if status_code == 200:
            data = json.loads(body)
            print(f"Retrieved {len(data)} strategies from API!")
            for idx, s in enumerate(data[:5]):
                print(f"  - Strat {idx+1}: name='{s.get('name')}' | status={s.get('status')} | mode={s.get('mode')} | broker={s.get('broker')}")
            if len(data) > 5:
                print(f"  ... and {len(data) - 5} more.")
        else:
            print(f"API Error Response: {body}")
except urllib.error.HTTPError as e:
    print(f"HTTP Error: {e.code} {e.reason}")
    print(f"Response body: {e.read().decode('utf-8')}")
except Exception as e:
    print(f"Error connecting to backend API: {e}")
