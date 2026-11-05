import asyncio
import json
import time
import os
import base64
import hashlib
from pathlib import Path
import aiohttp
from typing import Dict, List

TOKENS_FILE = Path(__file__).resolve().parent.parent / "tokens.json"

DEVICE_PROFILES = {
    "pc": {"$os": "Windows", "$browser": "Discord Client", "$device": "pc"},
    "ios": {"$os": "iOS", "$browser": "Discord iOS", "$device": "iPhone"},
    "android": {"$os": "Android", "$browser": "Discord Android", "$device": "Discord Android"},
    "vr": {"$os": "Android", "$browser": "Discord VR", "$device": "Oculus Quest 2"}
}

COMMON_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) discord/1.0.9168 Chrome/128.0.6613.186 Electron/32.2.2 Safari/537.36"

def get_token_id(token: str) -> str:
    """Opaque ID so raw token is never exposed to frontend."""
    return "acc_" + hashlib.sha256(token.strip().encode()).hexdigest()[:12]

def get_super_props(platform="pc"):
    plat = platform.lower()
    data = {
        "os": "Windows" if plat == "pc" else "iOS" if plat == "ios" else "Android",
        "browser": "Discord Client" if plat == "pc" else "Discord iOS" if plat == "ios" else "Discord Android",
        "release_channel": "stable",
        "client_version": "1.0.9168",
        "os_version": "10.0.22631",
        "os_arch": "x64",
        "system_locale": "en-US",
        "client_build_number": 343349
    }
    return base64.b64encode(json.dumps(data).encode()).decode()

def load_data() -> dict:
    env_tokens = os.getenv("TOKENS_JSON")
    if env_tokens:
        try:
            return json.loads(env_tokens)
        except Exception:
            pass

    if TOKENS_FILE.exists():
        try:
            with open(TOKENS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_data(data: dict):
    try:
        with open(TOKENS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def resolve_image(val: str) -> str:
    if not val: return None
    v = str(val).strip()
    if v.startswith("http://") or v.startswith("https://"):
        return f"mp:external/{v}"
    return v

class DiscordClient:
    def __init__(self, token: str, config: dict, fleet_manager):
        self.token = token.strip().strip('"').strip("'")
        self.acc_id = get_token_id(self.token)
        self.config = config
        self.fleet_manager = fleet_manager
        
        self.user_id = None
        self.session_id = None
        self.user_tag = config.get("tag") or "Connecting..."
        self.avatar_url = "https://cdn.discordapp.com/embed/avatars/0.png"
        
        self.ws = None
        self.session = None
        self.heartbeat_task = None
        self.running = False
        
        self.is_connected = False
        
        # 24/7 Voice details
        self.vc_connected = False
        self.vc_guild_id = None
        self.vc_channel_id = None
        self.vc_guild_name = None
        self.vc_channel_name = None
        self.vc_guild_icon = None
        
        # In-memory Gateway Cache (NO HTTP CALLS)
        self.guilds_cache = {} 
        self.internal_start_time = int(time.time())

    async def start(self):
        self.running = True
        headers = {
            "User-Agent": COMMON_UA,
            "X-Super-Properties": get_super_props(self.config.get("platform", "pc"))
        }
        self.session = aiohttp.ClientSession(headers=headers)
        while self.running:
            try:
                await self.connect()
            except asyncio.CancelledError:
                break
            except Exception:
                self.is_connected = False
                await asyncio.sleep(5)

    async def stop(self):
        self.running = False
        if self.heartbeat_task: self.heartbeat_task.cancel()
        if self.ws and not self.ws.closed: await self.ws.close()
        if self.session and not self.session.closed: await self.session.close()
        self.is_connected = False

    async def restart(self):
        await self.stop()
        await asyncio.sleep(0.5)
        asyncio.create_task(self.start())

    async def connect(self):
        uri = "wss://gateway.discord.gg/?v=10&encoding=json"
        async with self.session.ws_connect(uri) as ws:
            self.ws = ws
            hello = await ws.receive_json()
            interval = hello["d"]["heartbeat_interval"]
            
            if self.heartbeat_task: self.heartbeat_task.cancel()
            self.heartbeat_task = asyncio.create_task(self.heartbeat(interval))
            
            await self.identify()
            
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    t, d = data.get("t"), data.get("d", {})
                    
                    if t == "READY":
                        self.user_id = str(d["user"]["id"])
                        self.session_id = str(d.get("session_id"))
                        discrim = d["user"].get("discriminator", "0")
                        self.user_tag = d["user"]["username"] if discrim == "0" else f"{d['user']['username']}#{discrim}"
                        if d["user"].get("avatar"):
                            self.avatar_url = f"https://cdn.discordapp.com/avatars/{self.user_id}/{d['user']['avatar']}.png"
                        
                        # Cache guilds naturally from Gateway (NO HTTP REQUESTS)
                        for g in d.get("guilds", []):
                            gid = str(g["id"])
                            self.guilds_cache[gid] = {
                                "id": gid,
                                "name": g.get("name", "Server"),
                                "icon": f"https://cdn.discordapp.com/icons/{gid}/{g['icon']}.png" if g.get("icon") else None,
                                "channels": {str(c["id"]): c.get("name") for c in g.get("channels", [])}
                            }

                        self.is_connected = True
                        print(f"[ONLINE] {self.user_tag} connected safely.")
                        
                        await self.apply_presence()
                        await self.apply_voice()

                    elif t == "GUILD_CREATE":
                        gid = str(d.get("id"))
                        self.guilds_cache[gid] = {
                            "id": gid,
                            "name": d.get("name", "Server"),
                            "icon": f"https://cdn.discordapp.com/icons/{gid}/{d['icon']}.png" if d.get("icon") else None,
                            "channels": {str(c["id"]): c.get("name") for c in d.get("channels", [])}
                        }

                    elif t == "VOICE_STATE_UPDATE":
                        if str(d.get("user_id")) == self.user_id:
                            cid, gid = d.get("channel_id"), d.get("guild_id")
                            if cid and gid:
                                self.vc_connected = True
                                self.vc_guild_id = str(gid)
                                self.vc_channel_id = str(cid)
                                # Pull names from in-memory cache directly (NO HTTP CALLS)
                                g_info = self.guilds_cache.get(str(gid), {})
                                self.vc_guild_name = g_info.get("name", f"Server {gid}")
                                self.vc_guild_icon = g_info.get("icon")
                                self.vc_channel_name = g_info.get("channels", {}).get(str(cid), f"Voice {cid}")
                            else:
                                self.vc_connected = False
                                self.vc_guild_name, self.vc_channel_name, self.vc_guild_icon = None, None, None
                                if self.config.get("voice", {}).get("enabled"):
                                    asyncio.create_task(self.auto_rejoin_voice())

                    elif t == "PRESENCE_UPDATE":
                        if str(d.get("user", {}).get("id")) == self.user_id:
                            st = d.get("status")
                            if st and st != self.config.get("status"):
                                self.config["status"] = st
                                save_data(self.fleet_manager.data)

                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

    async def heartbeat(self, interval):
        while self.running:
            await asyncio.sleep(interval / 1000)
            if self.ws and not self.ws.closed:
                try:
                    await self.ws.send_json({"op": 1, "d": None})
                except Exception:
                    break

    async def identify(self):
        plat = self.config.get("platform", "pc").lower()
        props = DEVICE_PROFILES.get(plat, DEVICE_PROFILES["pc"])
        status = self.config.get("status", "online").lower()
        
        await self.ws.send_json({
            "op": 2,
            "d": {
                "token": self.token,
                "capabilities": 16381,
                "properties": props,
                "presence": {
                    "status": status,
                    "since": 0,
                    "activities": [],
                    "afk": status == "idle"
                }
            }
        })

    async def apply_presence(self):
        if not self.ws or self.ws.closed: return
        status = self.config.get("status", "online").lower()
        custom_text = self.config.get("custom_status", "").strip()
        rpc = self.config.get("rpc", {})

        activities = []
        if custom_text:
            activities.append({"type": 4, "name": "Custom Status", "state": custom_text, "id": "custom"})

        rpc_name = str(rpc.get("name", "")).strip()
        if rpc_name:
            act_type_map = {"playing": 0, "streaming": 1, "listening": 2, "watching": 3, "competing": 5}
            act_type = act_type_map.get(str(rpc.get("type", "playing")).lower(), 0)
            
            act = {"type": act_type, "name": rpc_name}
            if act_type == 1:
                act["url"] = str(rpc.get("url") or "https://twitch.tv/discord").strip()
            if rpc.get("application_id"): act["application_id"] = str(rpc.get("application_id")).strip()
            if rpc.get("details"): act["details"] = str(rpc.get("details")).strip()
            if rpc.get("state"): act["state"] = str(rpc.get("state")).strip()

            ts = {}
            s_raw = str(rpc.get("start_time", "")).strip().lower()
            if s_raw in ("auto", "true"):
                ts["start"] = self.internal_start_time * 1000
            elif s_raw.isdigit():
                val = int(s_raw)
                ts["start"] = val * 1000 if val < 10000000000 else val
            if ts: act["timestamps"] = ts

            assets = {}
            lg = resolve_image(rpc.get("large_image"))
            if lg: assets["large_image"] = lg
            sm = resolve_image(rpc.get("small_image"))
            if sm: assets["small_image"] = sm
            if assets: act["assets"] = assets

            activities.append(act)

        await self.ws.send_json({
            "op": 3,
            "d": {
                "since": 0,
                "activities": activities,
                "status": status,
                "afk": status == "idle"
            }
        })

    # ── 24/7 VOICE JOIN ──
    async def apply_voice(self):
        if not self.ws or self.ws.closed: return
        voice = self.config.get("voice", {})
        enabled = voice.get("enabled", False)
        gid = str(voice.get("guild_id", "")).strip()
        cid = str(voice.get("channel_id", "")).strip()

        if enabled and gid and cid:
            await self.ws.send_json({
                "op": 4,
                "d": {
                    "guild_id": gid,
                    "channel_id": cid,
                    "self_mute": voice.get("self_mute", True),
                    "self_deaf": voice.get("self_deaf", True),
                    "self_video": False
                }
            })
        else:
            if gid:
                await self.ws.send_json({"op": 4, "d": {"guild_id": gid, "channel_id": None, "self_mute": True, "self_deaf": False}})
            self.vc_connected = False
            self.vc_guild_name, self.vc_channel_name = None, None

    async def auto_rejoin_voice(self):
        await asyncio.sleep(4)
        if self.running and self.config.get("voice", {}).get("enabled"):
            await self.apply_voice()

class DiscordFleetManager:
    def __init__(self):
        self.clients: Dict[str, DiscordClient] = {}
        self.data: dict = load_data()

    def get_token_by_id(self, acc_id: str) -> str:
        for token in self.data.keys():
            if get_token_id(token) == acc_id:
                return token
        return None

    async def start_all(self):
        for token, cfg in self.data.items():
            client = DiscordClient(token, cfg, self)
            self.clients[token] = client
            asyncio.create_task(client.start())

    async def add_token(self, token: str, tag: str = None) -> bool:
        clean = token.strip().strip('"').strip("'")
        if clean in self.clients:
            if tag:
                self.clients[clean].config["tag"] = tag
                self.data[clean]["tag"] = tag
                save_data(self.data)
            await self.clients[clean].restart()
            return True

        cfg = {
            "tag": tag or "Discord User",
            "status": "online",
            "platform": "pc",
            "custom_status": "",
            "rpc": {"name": "", "type": "playing", "details": "", "state": "", "large_image": "", "small_image": "", "start_time": "auto"},
            "voice": {"enabled": False, "guild_id": "", "channel_id": "", "self_mute": True, "self_deaf": True}
        }
        self.data[clean] = cfg
        save_data(self.data)

        client = DiscordClient(clean, cfg, self)
        self.clients[clean] = client
        asyncio.create_task(client.start())
        return True

    async def remove_by_id(self, acc_id: str):
        token = self.get_token_by_id(acc_id)
        if not token: return
        if token in self.clients:
            await self.clients[token].stop()
            del self.clients[token]
        if token in self.data:
            del self.data[token]
            save_data(self.data)

    async def update_by_id(self, acc_id: str, new_cfg: dict):
        token = self.get_token_by_id(acc_id)
        if not token or token not in self.clients: return

        old_plat = self.data[token].get("platform", "pc")
        new_plat = new_cfg.get("platform", old_plat)

        for k, v in new_cfg.items():
            if isinstance(v, dict) and k in self.data[token]:
                self.data[token][k].update(v)
            else:
                self.data[token][k] = v

        save_data(self.data)
        client = self.clients[token]
        client.config = self.data[token]

        if old_plat != new_plat:
            await client.restart()
        else:
            await client.apply_presence()
            await client.apply_voice()

    def get_accounts_list(self) -> List[dict]:
        res = []
        for token, client in self.clients.items():
            cfg = client.config
            res.append({
                "id": client.acc_id,
                "tokenMask": f"••••••••{token[-4:]}" if len(token) > 8 else "••••••••",
                "tag": client.user_tag,
                "avatar": client.avatar_url,
                "online": client.is_connected,
                "status": cfg.get("status", "online"),
                "platform": cfg.get("platform", "pc"),
                "custom_status": cfg.get("custom_status", ""),
                "rpc": cfg.get("rpc", {}),
                "voice": cfg.get("voice", {}),
                "voiceConnected": client.vc_connected,
                "vc_guild_name": client.vc_guild_name,
                "vc_channel_name": client.vc_channel_name,
                "guilds": list(client.guilds_cache.values())
            })
        return res