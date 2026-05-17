import requests

API = 'http://127.0.0.1:8000/api'
for email in ['test+1@example.com', 'test+2@example.com']:
    try:
        r = requests.post(f'{API}/auth/register', json={'email': email, 'password': 'password123', 'name': 'Test User'})
        print('register', email, r.status_code, r.text)
        if r.status_code == 200:
            token = r.json().get('access_token')
            r2 = requests.post(f'{API}/auth/login', json={'email': email, 'password': 'password123'})
            print('login', email, r2.status_code, r2.text)
    except Exception as e:
        print('error', e)
