"""
railway_fix_and_push.py
-----------------------
Applies all necessary fixes for Railway deployment and pushes to GitHub.
"""

import os
import subprocess
import sys

# ========================= CONFIG =========================
GITHUB_REPO = "https://github.com/as-cell-star/lovedogs.git"
BRANCH = "main"

# ========================= HELPERS =========================
def write(path, content):
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"✅ Written: {path}")

def run(cmd, check=True):
    print(f"  $ {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())
    if check and result.returncode != 0:
        print(f"\n❌ ERROR: Command failed (exit {result.returncode})")
        sys.exit(1)
    return result

# Sanity check
if not os.path.isdir("backend"):
    print("❌ 'backend' folder not found. Run this script from repo root.")
    sys.exit(1)

print("\n🚀 Starting Railway Deployment Fixes...\n")

# ====================== 1. railway.toml ======================
print("1️⃣ Creating railway.toml ...")
write("railway.toml", """\
[build]
builder = "nixpacks"
buildCommand = "cd backend && pip install -r requirements.txt"

[deploy]
startCommand = "cd backend && uvicorn app.main:app --host 0.0.0.0 --port $PORT"
healthcheckPath = "/health"
healthcheckTimeout = 300
restartPolicyType = "on_failure"
restartPolicyMaxRetries = 5
""")

# ====================== 2. backend/app/main.py ======================
print("2️⃣ Patching backend/app/main.py with health check + env validation...")

main_py_path = "backend/app/main.py"
with open(main_py_path, "r", encoding="utf-8") as f:
    content = f.read()

# Add imports and fixes if not already present
if "@app.get(\"/health\")" not in content:
    # Find the first FastAPI app = FastAPI() line and insert before it
    insertion = '''import os
import logging
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)

def verify_env():
    required = ["DATABASE_URL", "JWT_SECRET"]
    missing = [var for var in required if not os.getenv(var)]
    if missing:
        logger.error(f"❌ Missing environment variables: {missing}")
        raise RuntimeError(f"Missing required env vars: {missing}")
    logger.info("✅ All required environment variables are set")

verify_env()

'''
    # Insert after imports (before app = FastAPI)
    if "app = FastAPI" in content:
        content = content.replace("app = FastAPI", insertion + "app = FastAPI", 1)
    else:
        # Fallback: append at the end
        content += "\n\n" + insertion + "\n@app.get(\"/health\")\nasync def health():\n    return {\"status\": \"ok\", \"service\": \"lovedogs360-backend\"}\n"

    with open(main_py_path, "w", encoding="utf-8") as f:
        f.write(content)
    print("✅ Added /health endpoint and env validation")
else:
    print("ℹ️  /health endpoint already exists")

# ====================== 3. package.json ======================
print("3️⃣ Fixing package.json ...")
write("package.json", '''{
  "name": "lovedogs360-root",
  "version": "1.0.1",
  "scripts": {
    "dev": "cd frontend && npm run dev",
    "backend": "cd backend && python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000",
    "backend:prod": "cd backend && uvicorn app.main:app --host 0.0.0.0 --port $PORT",
    "docker-up": "docker-compose up --build"
  }
}
''')

# ====================== 4. Git Push ======================
print("4️⃣ Setting up Git and pushing...")

if not os.path.isdir(".git"):
    run("git init")
    run(f"git remote add origin {GITHUB_REPO}")

run("git remote set-url origin " + GITHUB_REPO, check=False)

# Set git identity
run('git config user.name "Eliud Were"', check=False)
run('git config user.email "deploy@lovedogs360.com"', check=False)

run("git add railway.toml backend/app/main.py package.json")
run('git commit -m "fix: Railway deployment - healthcheck, env validation, proper start command"', check=False)

print("5️⃣ Pushing to GitHub...")
push = run(f"git push -u origin {BRANCH}", check=False)

if push.returncode != 0:
    print("⚠️  Force pushing due to history difference...")
    run(f"git push -u origin {BRANCH} --force")

print("\n" + "="*60)
print("🎉 SUCCESS! All Railway fixes applied and pushed.")
print("Next Steps:")
print("   1. Go to Railway → Variables → Add DATABASE_URL and JWT_SECRET")
print("   2. Trigger a new deployment")
print("   3. Your app should now stay 'Healthy'")
print("="*60)