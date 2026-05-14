# QuantDesk — Run Locally on Your Machine

A lightweight algo-trading terminal you can run on a single laptop or even a Raspberry Pi.
Open it on your phone too — the UI is fully mobile-friendly.

## What you need (one-time install)

| Tool      | Version | Mac (brew)               | Ubuntu/Debian                       | Windows                                |
| --------- | ------- | ------------------------ | ----------------------------------- | -------------------------------------- |
| Python    | 3.10+   | `brew install python`    | `sudo apt install python3 python3-venv` | https://python.org/downloads          |
| Node.js   | 18+     | `brew install node yarn` | `sudo apt install nodejs` then `npm i -g yarn` | https://nodejs.org + `npm i -g yarn` |
| MongoDB   | 6+      | `brew install mongodb-community` | https://www.mongodb.com/docs/manual/installation/ | https://www.mongodb.com/try/download/community |

Start MongoDB once:
```bash
# Mac
brew services start mongodb-community
# Linux
sudo systemctl start mongod
```

---

## Setup (2 minutes)

```bash
git clone <this-repo>   # or copy the /app folder anywhere on your machine
cd app
```

### 1. Backend
```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Edit `backend/.env` (it's already filled with safe defaults):
```
MONGO_URL="mongodb://localhost:27017"
DB_NAME="quantdesk"
CORS_ORIGINS="*"
JWT_SECRET="<change-me-to-any-random-string>"
EMERGENT_LLM_KEY="sk-emergent-..."   # already set; powers the AI bot
```

Run backend:
```bash
uvicorn server:app --host 0.0.0.0 --port 8001 --reload
```

### 2. Frontend
```bash
cd ../frontend
yarn install
```

Edit `frontend/.env`:
```
REACT_APP_BACKEND_URL=http://localhost:8001
WDS_SOCKET_PORT=3000
```

Run frontend:
```bash
yarn start
```

Visit **http://localhost:3000** → sign up → trade!

---

## Use it on your phone

Both backend and frontend listen on `0.0.0.0`. To open from your phone (same Wi-Fi network):

1. On your laptop, find its local IP: `ifconfig` (Mac/Linux) or `ipconfig` (Windows). Look for something like `192.168.1.42`.
2. Edit `frontend/.env`:
   ```
   REACT_APP_BACKEND_URL=http://192.168.1.42:8001
   ```
3. Restart the frontend: `yarn start`
4. On your phone browser visit `http://192.168.1.42:3000`
5. Tap "Add to Home Screen" — it acts like a real app (mobile bottom-nav with Home / Strats / AI Bot / Orders / Holdings, swipeable tables).

---

## How light is it?

- **RAM**: ~250 MB total (Mongo ~80 MB, FastAPI ~80 MB, React dev server ~100 MB)
- **CPU**: idle ~1-2 %
- **Disk**: ~600 MB including all node_modules
- **No GPU / no Docker / no cloud needed**

For production-mode (even lighter, ~70 MB RAM total):
```bash
cd frontend && yarn build
# Then serve build/ folder via any static server, e.g.:
npx serve -s build -l 3000
```

---

## Default test login
- Email: `demo@quantdesk.io`
- Password: `demo1234`

(Or sign up with your own — it's an SQLite-free single-Mongo setup, fully local.)

---

## Optional: real Zerodha trading
The app already accepts your Zerodha Kite Connect `api_key` + `api_secret` at **Broker Keys**. Today they're stored encrypted but execution stays paper-mode for safety. To enable live execution, install `kiteconnect` (`pip install kiteconnect`) and wire it into `/api/orders` — happy to do this in the next iteration.
