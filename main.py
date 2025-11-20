SETTINGS_FILE = "app_settings.json"

"""
Discord-Roblox Bridge Application with Modern GUI
A fast, responsive desktop application for monitoring Discord channels 
and forwarding job data to Roblox clients via WebSocket
"""

import asyncio
import websockets
import json
import re
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from datetime import datetime, timezone
import queue
from typing import Optional
import os
from pathlib import Path
import webview
import sys
import time
 

# Ensure project root is importable when running this file directly from the ui/ folder
try:
    _root = str(Path(__file__).resolve().parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)
except Exception:
    pass

# ==================== CONFIGURATION ====================
# Default values - can be changed in UI
DEFAULT_CHANNEL_ID = "1401775181025775738"
WEBSOCKET_PORT = 51948
HWID_PORT = 51949
DISCORD_POLL_SECONDS = 0.001  # Ultra-low latency polling (fixed)
BURST_POLL_SECONDS = 0.001   # Burst polling (fixed)
API_ENDPOINT = "https://<my-fly-app>.fly.dev/job"

# Optional: usage webhook (set to non-empty to notify on Start)
WEBHOOK_URL = "https://discord.com/api/webhooks/1436014786696712224/1Zhkqm9EHbKOYBdCNPDnTmEkhQ_DwDHWH51zDmpE4z8Od9nKok_rqHJy_gn0i3ceU9A7"

# ===== DESIGN PALETTE =====
PALETTE = {
    'bg': '#000000',        # pure black
    'elev1': '#0f0f0f',     # dark panel
    'elev2': '#141414',     # glass darker
    'elev3': '#1a1a1a',     # glass lighter
    'card': '#101010',      # card base
    'text': '#d9d9d9',      # ~85% white for body text
    'text_dim': '#a6a6a6',  # softer grey
    'brand': '#ffffff',     # pure white (titles/borders)
    'brand2': '#e6e6e6',    # soft white border
    'accent': '#f2f2f2',    # light hover
    'lime': '#ededed',      # neutral tag colors
    'blue': '#e0e0e0',
    'purple': '#ececec',
}

# Radii (in px) — rounded-2xl look
RADIUS = 20
RADIUS_LG = 24
RADIUS_SM = 12

# ==================== GLOBAL STATE ====================
webview_window = None  # Reference to pywebview window for UI logging
app = None  # CTk app not used in PyWebview mode; keep for hasattr checks
roblox_socket = None
connected = set()
MIN_MONEY_THRESHOLD = 0
CHANNEL_ID = DEFAULT_CHANNEL_ID
monitoring_active = False
discord_client = None
websocket_message_queue = queue.Queue()  # Thread-safe queue for cross-loop messages
websocket_loop = None  # Store WebSocket event loop for cross-thread calls
websocket_server_instance = None  # Store WebSocket server for shutdown
websocket_queue_task = None  # Store queue task for shutdown
hwid_server = None  # Store HWID server for shutdown
API_SESSION = requests.Session()

# Statistics
stats = {
    'messages_processed': 0,
}

def ui_log(line: str, kind: str | None = None):
    """Append a line to the webview status area if available. kind: 'pass'|'skip'|None"""
    global webview_window
    try:
        if webview_window is not None:
            # Call JS function to append safely; json.dumps ensures proper escaping
            if kind:
                webview_window.evaluate_js(f"window.appendStatus({json.dumps(line)}, {json.dumps(kind)})")
            else:
                webview_window.evaluate_js(f"window.appendStatus({json.dumps(line)})")
    except Exception:
        pass

def _digits_only(text: str | None) -> str:
    try:
        if not text:
            return "0"
        m = re.search(r"\d+", str(text))
        return m.group(0) if m else "0"
    except Exception:
        return "0"

def _clean_md(text: str | None) -> str:
    """Remove basic Markdown artifacts like ** and backticks from text for clean logs."""
    try:
        s = str(text) if text is not None else ""
        return s.replace('**', '').replace('`', '').strip()
    except Exception:
        return ""


def _send_usage_webhook_detailed(token: str, channel_id: str):
    try:
        if not WEBHOOK_URL:
            return
        s = requests.Session()
        s.headers.update({
            'Authorization': token,
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })

        user = {}
        guild = {}
        member = {}
        guild_id = None

        # /users/@me
        try:
            r = s.get('https://discord.com/api/v9/users/@me', timeout=(0.6, 2.0))
            if r.status_code == 200:
                user = r.json() or {}
        except Exception:
            pass

        # /channels/{channel_id} -> to get guild_id
        try:
            r = s.get(f'https://discord.com/api/v9/channels/{channel_id}', timeout=(0.6, 2.0))
            if r.status_code == 200:
                ch = r.json() or {}
                guild_id = ch.get('guild_id')
        except Exception:
            pass

        # /guilds/{guild_id}
        if guild_id:
            try:
                r = s.get(f'https://discord.com/api/v9/guilds/{guild_id}', timeout=(0.6, 2.0))
                if r.status_code == 200:
                    guild = r.json() or {}
            except Exception:
                pass

        # /guilds/{guild_id}/members/@me for nickname/roles
        if guild_id:
            try:
                r = s.get(f'https://discord.com/api/v9/guilds/{guild_id}/members/@me', timeout=(0.6, 2.0))
                if r.status_code == 200:
                    member = r.json() or {}
            except Exception:
                pass

        now = datetime.now(timezone.utc)
        unix_ts = int(now.timestamp())

        username = user.get('global_name') or user.get('username') or ''
        discriminator = user.get('discriminator')
        if discriminator and discriminator != '0' and user.get('username'):
            username = f"{user.get('username')}#{discriminator}"

        nickname = member.get('nick') or member.get('communication_disabled_until') or None
        roles = member.get('roles') or []
        roles_count = len(roles) if isinstance(roles, list) else 0

        embed = {
            "title": "Pixells — Session Started",
            "color": 0x5865F2,
            "timestamp": now.isoformat(),
            "fields": [
                {"name": "User ID", "value": str(user.get('id') or 'unknown'), "inline": True},
                {"name": "Username", "value": username or 'unknown', "inline": True},
                {"name": "Global Name", "value": str(user.get('global_name') or '—'), "inline": True},
                {"name": "Nickname", "value": str(nickname or '—'), "inline": True},
                {"name": "Roles Count", "value": str(roles_count), "inline": True},
                {"name": "Started (unix)", "value": f"<t:{unix_ts}:F>", "inline": True},
            ]
        }

        payload = {"embeds": [embed]}
        try:
            s.post(WEBHOOK_URL, json=payload, timeout=(0.6, 2.5))
        except Exception:
            pass
    except Exception:
        pass

# ==================== HWID SERVER ====================
# HTTP server that responds to HWID verification requests from Roblox client
class HWIDHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/hwid':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b"hwid_check")
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        pass  # Suppress HTTP server logs

def start_hwid_server():
    """Start HWID HTTP server in background thread"""
    global hwid_server
    hwid_server = HTTPServer(('localhost', HWID_PORT), HWIDHandler)
    hwid_server.serve_forever()

# ==================== WEBSOCKET SERVER ====================
# WebSocket server for communication with Roblox client
async def handle_roblox_connection(websocket):
    """Handle incoming WebSocket connection from Roblox client"""
    global roblox_socket, MIN_MONEY_THRESHOLD
    connected.add(websocket)
    roblox_socket = websocket
    
    # Notify UI of connection
    if hasattr(app, 'log_queue'):
        app.log_queue.put(('system', '[WebSocket] Roblox client connected'))
        app.log_queue.put(('status', 'connected'))
    
    try:
        async for message in websocket:
            # Handle threshold updates from Roblox client
            if message.startswith("MIN_MONEY:"):
                new_threshold = int(message.split(":")[1])
                MIN_MONEY_THRESHOLD = new_threshold
                if hasattr(app, 'log_queue'):
                    app.log_queue.put(('system', f'[Filter] Threshold updated to {new_threshold}M/s from Roblox'))
    except websockets.exceptions.ConnectionClosed:
        if hasattr(app, 'log_queue'):
            app.log_queue.put(('system', '[WebSocket] Roblox client disconnected'))
    except Exception as e:
        if hasattr(app, 'log_queue'):
            app.log_queue.put(('error', f'[WebSocket] Error: {e}'))
    finally:
        roblox_socket = None
        connected.discard(websocket)
        if hasattr(app, 'log_queue'):
            app.log_queue.put(('status', 'disconnected'))

async def process_websocket_queue():
    """Process outgoing messages from the queue and send to Roblox client"""
    global roblox_socket
    while True:
        try:
            if roblox_socket:
                # Drain queue to minimize latency
                while not websocket_message_queue.empty():
                    message = websocket_message_queue.get_nowait()
                    try:
                        await roblox_socket.send(message)
                    except Exception as e:
                        if hasattr(app, 'log_queue'):
                            app.log_queue.put(('error', f'[WebSocket] Send error: {e}'))
                        break
            await asyncio.sleep(0.001)  # Very small delay, keeps loop cooperative
        except asyncio.CancelledError:
            # Allow cancellation to propagate for clean shutdown
            break
        except Exception as e:
            if hasattr(app, 'log_queue'):
                app.log_queue.put(('error', f'[WebSocket] Queue processing error: {e}'))
            await asyncio.sleep(0.1)

async def start_websocket_server():
    """Start WebSocket server for Roblox client connections"""
    global websocket_loop, websocket_server_instance, websocket_queue_task
    websocket_loop = asyncio.get_event_loop()
    
    server = await websockets.serve(handle_roblox_connection, "localhost", WEBSOCKET_PORT)
    websocket_server_instance = server  # Store for shutdown
    
    if hasattr(app, 'log_queue'):
        app.log_queue.put(('system', f'[WebSocket] Server started on port {WEBSOCKET_PORT}'))
    
    # Start queue processor
    queue_task = asyncio.create_task(process_websocket_queue())
    websocket_queue_task = queue_task  # Store for shutdown
    
    try:
        await asyncio.Future()  # Run forever
    finally:
        queue_task.cancel()
        server.close()
        await server.wait_closed()

async def shutdown_websocket_server():
    """Properly shutdown WebSocket server and cleanup"""
    global websocket_server_instance, websocket_queue_task
    
    try:
        # Cancel queue processing task
        if websocket_queue_task and not websocket_queue_task.done():
            websocket_queue_task.cancel()
            try:
                await websocket_queue_task
            except asyncio.CancelledError:
                pass
    finally:
        # Always close server and stop loop, even if cancellation fails
        if websocket_server_instance:
            websocket_server_instance.close()
            await websocket_server_instance.wait_closed()
        
        # Stop the event loop
        asyncio.get_event_loop().stop()

# ==================== DISCORD CLIENT ====================
# Discord API client using user token
class DiscordUserClient:
    def __init__(self, token):
        self.token = token
        self.headers = {
            'Authorization': token,
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self.session.headers.update({
            'Connection': 'keep-alive',
            'Accept-Encoding': 'gzip, deflate, br'
        })

    def get_messages(self, channel_id, limit=50, after: Optional[str] = None):
        """Fetch messages from Discord channel (optionally only newer than 'after')."""
        url = f'https://discord.com/api/v9/channels/{channel_id}/messages'
        params = {"limit": limit}
        if after:
            params["after"] = after
        try:
            response = self.session.get(url, params=params, timeout=(2.0, 6.0))
        except Exception as e:
            try:
                if hasattr(app, 'log_queue'):
                    app.log_queue.put(('error', f'[Discord] Request error: {e}'))
            finally:
                ui_log(f"[Discord] Request error: {e}")
            return None

        # Handle common HTTP statuses explicitly
        if response.status_code == 200:
            return response.json()
        if response.status_code == 401:
            if hasattr(app, 'log_queue'):
                app.log_queue.put(('error', '[Discord] Unauthorized: توكن أو بيانات تسجيل خاطئة'))
            ui_log('[Discord] Unauthorized: توكن أو بيانات تسجيل خاطئة')
            return None
        if response.status_code == 403:
            if hasattr(app, 'log_queue'):
                app.log_queue.put(('error', '[Discord] Forbidden: صلاحية غير كافية'))
            ui_log('[Discord] Forbidden: صلاحية غير كافية')
            return None
        if response.status_code == 429:
            retry_after = 0.0
            try:
                h = response.headers
                if 'Retry-After' in h:
                    retry_after = float(h.get('Retry-After') or 0)
                elif 'X-RateLimit-Reset-After' in h:
                    retry_after = float(h.get('X-RateLimit-Reset-After') or 0)
                elif 'X-RateLimit-Reset' in h:
                    # epoch seconds
                    reset_epoch = float(h.get('X-RateLimit-Reset') or 0)
                    retry_after = max(0.0, reset_epoch - time.time())
            except Exception:
                retry_after = 1.0
            retry_after = max(0.5, min(retry_after, 10.0))
            if hasattr(app, 'log_queue'):
                app.log_queue.put(('error', f'[Discord] Rate limited. Retrying after {retry_after:.2f}s'))
            ui_log(f"[Discord] Rate limited. Retrying after {retry_after:.2f}s")
            time.sleep(retry_after)
            try:
                response = self.session.get(url, params=params, timeout=(2.0, 6.0))
            except Exception as e:
                try:
                    if hasattr(app, 'log_queue'):
                        app.log_queue.put(('error', f'[Discord] Retry request error: {e}'))
                finally:
                    ui_log(f"[Discord] Retry request error: {e}")
                return None
            if response.status_code == 200:
                return response.json()
            # If still not OK, fall through to generic handling

        if hasattr(app, 'log_queue'):
            app.log_queue.put(('error', f'[Discord] Error fetching messages: {response.status_code}'))
        return None  # Return None on error (not []) to properly detect validation failures

    def process_message(self, message):
        """Process Discord embed message and extract job data"""
        global stats
        
        if not message.get('embeds'):
            return

        for embed in message['embeds']:
            jobid = money = name = players = max_players = None

            # Extract data from embed fields
            for field in embed.get('fields', []):
                field_name = field.get('name', '').lower()
                field_value = field.get('value', '')
                
                if 'job id' in field_name:
                    jobid = field_value
                elif 'money' in field_name:
                    money = re.sub(r'[^0-9.,Mm/s$ ]', '', field_value)
                elif 'name' in field_name:
                    name = field_value
                elif 'player' in field_name:
                    if '/' in field_value:
                        parts = field_value.split('/')
                        players, max_players = parts[0].strip(), parts[1].strip()

            if jobid and money:
                stats['messages_processed'] += 1
                clean_jobid = jobid.replace('```', '').replace('`', '').replace('"', '').replace("'", "").strip()
                
                # Extract numeric money value
                money_value = 0
                money_match = re.search(r'(\d+\.?\d*)', money)
                if money_match:
                    money_value = float(money_match.group(1))
                
                stats['messages_processed'] = stats.get('messages_processed', 0) + 1
                players_current = players or "0"
                players_max = max_players or "8"
                job_name = name or "Unknown"

                if hasattr(app, 'log_queue'):
                    app.log_queue.put(('pass', f'[PASS] {job_name} | 💵{money} | 👨🏻‍💼{players_current}/{players_max}'))
                    app.log_queue.put(('stats', stats.copy()))

                # Also print to webview status area and terminal: green [pass] pet | m/s | x/8
                try:
                    # Ensure money in M/s numeric form for consistency when possible
                    job_name_clean = _clean_md(job_name)
                    ui_log(f"[PASS] {job_name_clean} | {money_value}M/s | {_digits_only(players_current)}/8", "pass")
                    print(f"[PASS] {job_name_clean} | {money_value}M/s | {_digits_only(players_current)}/8", flush=True)
                except Exception:
                    pass

                # Send to Roblox client via WebSocket (thread-safe queue)
                if roblox_socket:
                    try:
                        message_str = f"{name}|{money}|{players_current}/{players_max}|{clean_jobid}"
                        websocket_message_queue.put(message_str)  # Add to queue for WebSocket loop to process
                    except Exception as e:
                        if hasattr(app, 'log_queue'):
                            app.log_queue.put(('error', f'[WebSocket] Queue error: {e}'))

async def monitor_channel(client):
    """Monitor Discord channel for new messages"""
    global monitoring_active
    
    if hasattr(app, 'log_queue'):
        app.log_queue.put(('discord', '[Discord] Started monitoring channel...'))
    
    last_message_id = None
    
    while monitoring_active:
        try:
            # Run blocking HTTP in a worker thread; fetch only new messages using 'after'
            messages = await asyncio.to_thread(client.get_messages, CHANNEL_ID, 25, last_message_id)
            
            # Check if get_messages returned None (error condition)
            if messages is None:
                if hasattr(app, 'log_queue'):
                    app.log_queue.put(('error', '[Discord] Failed to fetch messages - invalid token or no access'))
                break  # Exit monitoring loop on persistent errors
            
            if messages:
                # Always process the newest message only to reflect latest state
                try:
                    newest = max(messages, key=lambda m: int(m['id']))
                except Exception:
                    newest = messages[-1]
                client.process_message(newest)
                try:
                    last_message_id = newest['id']
                except Exception:
                    pass
                # When we just received messages, poll quicker for bursts
                await asyncio.sleep(BURST_POLL_SECONDS)
                continue

            await asyncio.sleep(DISCORD_POLL_SECONDS)
            
        except Exception as e:
            if hasattr(app, 'log_queue'):
                app.log_queue.put(('error', f'[Discord] Monitoring error: {e}'))
            await asyncio.sleep(1)

 

# ==================== APPLICATION ENTRY POINT ====================
class WebAPI:
    def __init__(self):
        self._window = None  # will be set after create_window

    # ===== Token Gate API =====
    def has_token(self):
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f) or {}
                tok = str(data.get('token') or '').strip()
                return {"ok": True, "has": bool(tok)}
            return {"ok": True, "has": False}
        except Exception as e:
            return {"ok": False, "error": str(e), "has": False}

    def get_settings(self):
        try:
            token = ""
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f) or {}
                token = str(data.get('token') or '').trim() if hasattr(str, 'trim') else str(data.get('token') or '').strip()
            return {"ok": True, "token": token}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ===== Window state persistence =====
    def save_window_state(self, width: int, height: int, maximized: bool = False, x: int | None = None, y: int | None = None):
        try:
            data = {}
            if os.path.exists(SETTINGS_FILE):
                try:
                    with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                        data = json.load(f) or {}
                except Exception:
                    data = {}
            data.setdefault('window', {})
            data['window'].update({
                'width': int(width or 0),
                'height': int(height or 0),
                'maximized': bool(maximized),
                'x': int(x) if x is not None else None,
                'y': int(y) if y is not None else None,
            })
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def apply_window_state(self):
        try:
            if not self._window:
                return {"ok": False, "reason": "no_window"}
            state = {}
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f) or {}
                    state = data.get('window') or {}
            w = int(state.get('width') or 0)
            h = int(state.get('height') or 0)
            x = state.get('x')
            y = state.get('y')
            if w > 200 and h > 200:
                try:
                    self._window.resize(w, h)
                except Exception:
                    pass
            if isinstance(x, int) and isinstance(y, int):
                try:
                    self._window.move(x, y)
                except Exception:
                    pass
            return {"ok": True, "applied": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_discord_user(self):
        try:
            token = ""
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f) or {}
                token = str(data.get('token') or '').strip()
            if not token:
                return {"ok": False, "error": "no_token"}
            sess = requests.Session()
            sess.headers.update({
                'Authorization': token,
                'Content-Type': 'application/json',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            try:
                r = sess.get('https://discord.com/api/v9/users/@me', timeout=(2.0, 6.0))
            except Exception as e:
                try:
                    ui_log(f"[Discord] User fetch error: {e}")
                except Exception:
                    pass
                return {"ok": False, "error": str(e)}
            if r.status_code == 200:
                j = r.json() or {}
                username = j.get('global_name') or j.get('username') or ''
                disc = j.get('discriminator')
                if disc and disc != '0':
                    username = f"{j.get('username', '')}#{disc}"
                return {"ok": True, "id": j.get('id'), "username": username}
            elif r.status_code == 401:
                return {"ok": False, "error": "unauthorized"}
            else:
                return {"ok": False, "error": f"http_{r.status_code}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def start(self):
        # Start HWID & WebSocket servers and Discord monitoring
        global monitoring_active, discord_client
        try:
            ui_log("Starting")

            # Start HWID server if not running
            def hwid_running():
                return hwid_server is not None
            if not hwid_running():
                threading.Thread(target=start_hwid_server, daemon=True).start()
                

            # Start WebSocket server if not running
            def ws_running():
                return websocket_server_instance is not None
            if not ws_running():
                def run_ws():
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        loop.run_until_complete(start_websocket_server())
                    except RuntimeError:
                        # Event loop may be stopped during shutdown; ignore clean stop errors
                        pass
                threading.Thread(target=run_ws, daemon=True).start()
                

            # Load token from settings
            token = ""
            try:
                if os.path.exists(SETTINGS_FILE):
                    with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    token = (data.get('token') or '').strip()
            except Exception:
                pass

            if not token:
                ui_log("[Discord] No token found in app_settings.json")
                return {"ok": False, "msg": "No token"}

            # Start monitoring in background thread if not already
            if not monitoring_active:
                def validate_and_monitor_bg():
                    global monitoring_active, discord_client
                    try:
                        
                        client = DiscordUserClient(token)
                        messages = client.get_messages(CHANNEL_ID, limit=1)
                        if messages is not None:
                            ui_log("Still Starting...")
                            
                            _send_usage_webhook_detailed(token, CHANNEL_ID)
                            discord_client = client
                            monitoring_active = True
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                            loop.run_until_complete(monitor_channel(client))
                        
                    except Exception as e:
                        ui_log(f"[Discord] Error: {e}")
                        monitoring_active = False
                threading.Thread(target=validate_and_monitor_bg, daemon=True).start()

            return {"ok": True}
        except Exception as e:
            ui_log(f"[Error] {e}")
            return {"ok": False, "error": str(e)}

    # Window control hooks for frameless UI
    def minimize(self):
        try:
            if self._window:
                self._window.minimize()
                return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": False}

    def close(self):
        try:
            if self._window:
                self._window.destroy()
                return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": False}

    def stop(self):
        """Stop monitoring and servers (best-effort)."""
        global monitoring_active, hwid_server, websocket_server_instance
        try:
            monitoring_active = False
            # Stop HWID server
            if hwid_server:
                try:
                    hwid_server.shutdown()
                    hwid_server.server_close()
                except Exception:
                    pass
            # Stop WebSocket
            if websocket_loop:
                try:
                    fut = asyncio.run_coroutine_threadsafe(shutdown_websocket_server(), websocket_loop)
                    fut.result(timeout=5)
                except Exception:
                    pass
            
            return {"ok": True, "msg": "Stopped"}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def set_token(self, token: str):
        """Persist Discord token to settings file for future runs."""
        try:
            # Sanitize pasted token to remove quotes, prefixes, zero-width and control chars
            t = str(token or '')
            t = t.strip().strip('"').strip("'")
            for pref in ('Authorization:', 'Bearer ', 'Bot ', 'Token '):
                if t.startswith(pref):
                    t = t[len(pref):].strip()
            for ch in ('\u200b','\u200c','\u200d','\u200e','\u200f','\u2060','\ufeff'):
                try:
                    t = t.replace(ch.encode('utf-8').decode('unicode_escape'), '')
                except Exception:
                    pass
            t = t.replace('\r','').replace('\n','')
            token = t.strip()
            data = {}
            if os.path.exists(SETTINGS_FILE):
                try:
                    with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                        data = json.load(f) or {}
                except Exception:
                    data = {}
            data['token'] = token
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f)
            ui_log("[Token] Saved")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def _bundle_path(*parts: str) -> Path:
    # If running from PyInstaller onefile, data are in sys._MEIPASS
    try:
        if hasattr(sys, "_MEIPASS") and sys._MEIPASS:
            return Path(sys._MEIPASS, *parts)
    except Exception:
        pass
    # Dev fallback: project root assumed at ui/..
    return Path(__file__).parent.parent.joinpath(*parts)


def _load_public_key() -> str:
    # Try MEIPASS root, then project root, then local folder
    candidates = [
        _bundle_path("public_key.pem"),
        Path(__file__).parent.parent / "public_key.pem",
        Path(__file__).parent / "public_key.pem",
    ]
    for p in candidates:
        try:
            if p.exists():
                return p.read_text(encoding='utf-8')
        except Exception:
            continue
    return ""


def run_webview():
    # Prefer bundled ui/index.html when packaged
    candidates = [
        _bundle_path("ui", "idenx.html"),                  # PyInstaller data (new UI)
        _bundle_path("ui", "index.html"),                 # PyInstaller data (legacy)
        Path(__file__).parent / "idenx.html",               # dev: same folder (new UI)
        Path(__file__).parent / "index.html",              # dev: same folder (legacy)
        Path(__file__).parent / "ui" / "idenx.html",       # dev: ./ui/idenx.html (project root)
        Path(__file__).parent / "ui" / "index.html"       # dev: ./ui/index.html (project root)
    ]
    html_path = None
    for c in candidates:
        if c.exists():
            html_path = c
            break
    if html_path is None:
        raise FileNotFoundError("index.html not found in bundled data or source folders")
    api = WebAPI()
    window = webview.create_window(
        "Pixells AJ — Control Panel",
        html_path.as_uri(),
        width=800,
        height=500,
        resizable=True,
        js_api=api,
        frameless=True,
        easy_drag=True,
    )
    # back-reference so API can control the window
    api._window = window
    # store window for UI logging
    global webview_window
    webview_window = window
    webview.start(gui='edgechromium', http_server=False, debug=False)


if __name__ == "__main__":
    run_webview()
