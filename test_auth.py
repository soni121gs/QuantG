import requests
import json

backend_url = "http://192.168.31.4:8000/api"

# Test 1: Check if auth routes exist
print("[TEST 1] Checking if auth routes exist...")
try:
    r = requests.get(f"{backend_url}/auth/me")
    print(f"  GET /auth/me: {r.status_code}")
except Exception as e:
    print(f"  Error: {e}")

# Test 2: Try login
print("\n[TEST 2] Attempting login...")
try:
    r = requests.post(
        f"{backend_url}/auth/login",
        json={"email": "test@example.com", "password": "password123"}
    )
    print(f"  Status: {r.status_code}")
    print(f"  Response: {r.text}")
except Exception as e:
    print(f"  Error: {e}")

# Test 3: Try register
print("\n[TEST 3] Attempting register...")
try:
    r = requests.post(
        f"{backend_url}/auth/register",
        json={"email": "newuser@example.com", "password": "password123", "name": "Test User"}
    )
    print(f"  Status: {r.status_code}")
    print(f"  Response: {r.text}")
except Exception as e:
    print(f"  Error: {e}")

# Test 4: Check CORS headers
print("\n[TEST 4] Checking CORS headers...")
try:
    r = requests.options(
        f"{backend_url}/auth/login",
        headers={
            "Origin": "http://192.168.31.4:3000",
            "Access-Control-Request-Method": "POST"
        }
    )
    print(f"  Status: {r.status_code}")
    print(f"  CORS Headers:")
    for k, v in r.headers.items():
        if "Access-Control" in k or "Allow" in k:
            print(f"    {k}: {v}")
except Exception as e:
    print(f"  Error: {e}")
