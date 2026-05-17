#!/usr/bin/env python3
"""Fix CORS and auth configuration"""

file_path = r"D:\Quant\QuantG\backend\server.py"

with open(file_path, 'r') as f:
    content = f.read()

# Fix CORS middleware to properly strip whitespace
old_cors = """app.add_middleware(
    CORSMiddleware,
    allow_credentials=False,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)"""

new_cors = """# Parse CORS origins properly - strip whitespace from comma-separated list
cors_origins = [o.strip() for o in os.environ.get('CORS_ORIGINS', '*').split(',') if o.strip()]
if not cors_origins:
    cors_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_credentials=False,
    allow_origins=cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)"""

if old_cors in content:
    content = content.replace(old_cors, new_cors)
    print("[OK] Fixed CORS middleware")
else:
    print("[WARN] CORS middleware pattern not found - it may already be fixed")

with open(file_path, 'w') as f:
    f.write(content)

print("[OK] Patch applied to server.py")
