import os
import sys
import json
import contextlib
from pathlib import Path
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

sys.path.append(str(Path(__file__).resolve().parent))
try:
    from discord_manager import DiscordFleetManager
except ImportError:
    from .discord_manager import DiscordFleetManager

CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.json"

def get_auth_config():
    env_user = os.getenv("ADMIN_USER")
    env_pass = os.getenv("ADMIN_PASS")
    if env_user and env_pass:
        return {"admin_user": env_user, "admin_pass": env_pass}

    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"admin_user": "admin", "admin_pass": "admin123"}

fleet_manager = DiscordFleetManager()

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await fleet_manager.start_all()
    yield

app = FastAPI(title="Studio Dashboard", lifespan=lifespan)
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

@app.head("/")
async def health_check():
    return Response(status_code=200)

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    html_file = FRONTEND_DIR / "index.html"
    if html_file.exists():
        return HTMLResponse(
            content=html_file.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"}
        )
    return "<h1>Frontend index.html not found!</h1>"

class LoginReq(BaseModel):
    username: str
    password: str

@app.post("/api/auth/login")
async def login(data: LoginReq):
    cfg = get_auth_config()
    if (data.username == cfg.get("admin_user")) and (data.password == cfg.get("admin_pass")):
        return {"success": True, "token": "session_auth_ok"}
    raise HTTPException(status_code=401, detail="Invalid username or password")

@app.get("/api/accounts")
async def get_accounts():
    return Response(
        content=json.dumps(fleet_manager.get_accounts_list()),
        media_type="application/json",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"}
    )

class AddTokenReq(BaseModel):
    token: str
    tag: str = "Discord User"

@app.post("/api/accounts/add")
async def add_account(data: AddTokenReq):
    success = await fleet_manager.add_token(data.token, data.tag)
    return {"success": success}

class SyncAllReq(BaseModel):
    config: dict

@app.post("/api/accounts/sync-all")
async def sync_all_accounts(data: SyncAllReq):
    for token in list(fleet_manager.clients.keys()):
        acc_id = fleet_manager.clients[token].acc_id
        await fleet_manager.update_by_id(acc_id, data.config)
    return {"success": True}

class UpdateReq(BaseModel):
    id: str
    config: dict

@app.post("/api/accounts/update")
async def update_account(data: UpdateReq):
    await fleet_manager.update_by_id(data.id, data.config)
    return {"success": True}

class DeleteReq(BaseModel):
    id: str

@app.post("/api/accounts/delete")
async def delete_account(data: DeleteReq):
    await fleet_manager.remove_by_id(data.id)
    return {"success": True}

class BulkStatusReq(BaseModel):
    status: str = "online"

@app.post("/api/bulk/status")
async def bulk_status(data: BulkStatusReq):
    for token in list(fleet_manager.clients.keys()):
        acc_id = fleet_manager.clients[token].acc_id
        await fleet_manager.update_by_id(acc_id, {"status": data.status})
    return {"success": True}

@app.post("/api/bulk/disconnect-vc")
async def bulk_disconnect_vc():
    for token in list(fleet_manager.clients.keys()):
        acc_id = fleet_manager.clients[token].acc_id
        await fleet_manager.update_by_id(acc_id, {"voice": {"enabled": False, "guild_id": "", "channel_id": ""}})
    return {"success": True}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("SERVER_PORT", os.getenv("PORT", 10507)))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)