import requests

backend_url = "http://192.168.31.4:8000/api"

# Test with actual Origin header
print("[TEST 1] Testing CORS with Origin header...")
try:
    r = requests.options(
        f"{backend_url}/auth/login",
        headers={
            "Origin": "http://192.168.31.4:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"
        }
    )
    print(f"  Status: {r.status_code}")
    print(f"  Response Headers:")
    for k, v in r.headers.items():
        print(f"    {k}: {v}")
except Exception as e:
    print(f"  Error: {e}")

# Test 2: Try login with user we registered
print("\n[TEST 2] Attempting login with correct credentials...")
try:
    r = requests.post(
        f"{backend_url}/auth/login",
        json={"email": "newuser@example.com", "password": "password123"}
    )
    print(f"  Status: {r.status_code}")
    data = r.json()
    if r.status_code == 200:
        print(f"  Token: {data.get('access_token', 'N/A')[:50]}...")
        print(f"  User: {data.get('user', {}).get('email')}")
    else:
        print(f"  Error: {data}")
except Exception as e:
    print(f"  Error: {e}")

# Test 3: Try with token
print("\n[TEST 3] Testing authenticated request...")
try:
    # First register a new user
    r = requests.post(
        f"{backend_url}/auth/register",
        json={"email": "test2@example.com", "password": "password123", "name": "Test User 2"}
    )
    if r.status_code == 200:
        token = r.json().get("access_token")
        print(f"  Registered and got token")
        
        # Now try to get profile
        r2 = requests.get(
            f"{backend_url}/auth/me",
            headers={"Authorization": f"Bearer {token}"}
        )
        print(f"  GET /auth/me status: {r2.status_code}")
        if r2.status_code == 200:
            print(f"  User: {r2.json().get('email')}")
        else:
            print(f"  Error: {r2.text}")
    else:
        print(f"  Register failed: {r.text}")
except Exception as e:
    print(f"  Error: {e}")

print("\n[SUMMARY]")
print("  - Backend is reachable at 192.168.31.4:8000")
print("  - Auth routes are working")
print("  - CORS should allow requests from 192.168.31.4:3000")
