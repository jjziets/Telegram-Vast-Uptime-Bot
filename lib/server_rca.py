#!/usr/bin/env python3
"""
Enhanced Uptime Bot Server with MTR/Hop Logging
- Simplified version with less CPU usage
- Stores hop data for RCA when workers go down
"""

import os
import json
import threading
import subprocess
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, jsonify, request, render_template_string, Response
from queue import Queue
from collections import defaultdict
import requests
import time
import re

app = Flask(__name__)

# Configuration
FAIL_TIMEOUT = int(os.getenv("FAIL_TIMEOUT", 180))
DATA_DIR = os.getenv("DATA_DIR", "/var/lib/uptime-bot")
HOP_LOG_FILE = os.path.join(DATA_DIR, "hops.jsonl")

# Simple in-memory storage
workers = {}  # worker_id -> {last_seen, ip, last_hop}
last_hop_data = {}  # worker_id -> last hop data dict
event_cache = []  # Recent events for analysis
message_queue = Queue()
lock = threading.Lock()

# Ensure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)

# === Authentication ===
def check_auth(username, password):
    return username == os.getenv("ADMIN_USER", "admin") and password == os.getenv("ADMIN_PASS", "admin")

def authenticate():
    return Response('Authentication required', 401, {'WWW-Authenticate': 'Basic realm="Admin"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

# === Helper Functions ===
def log_hop_data(worker_id, hop_data):
    """Append hop data to log file (non-blocking)"""
    try:
        with open(HOP_LOG_FILE, "a") as f:
            record = {"ts": datetime.utcnow().isoformat(), "worker": worker_id, **hop_data}
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        print(f"Error logging hop data: {e}")

def find_problem_hop(hop_data):
    """Find the hop causing issues"""
    hops = hop_data.get("hops", [])
    for hop in hops:
        if hop.get("loss", 0) > 50:
            return hop
    return None

def run_server_mtr(target_ip):
    """Run MTR from server to target IP to check reachability"""
    try:
        # Run quick MTR with 3 probes
        result = subprocess.run(
            ["mtr", "-r", "-c", "3", "-n", target_ip],
            capture_output=True, text=True, timeout=30
        )
        
        if result.returncode != 0:
            # Fallback to ping
            ping_result = subprocess.run(
                ["ping", "-c", "3", "-W", "2", target_ip],
                capture_output=True, text=True, timeout=10
            )
            if "0 received" in ping_result.stdout or ping_result.returncode != 0:
                return {"reachable": False, "method": "ping", "detail": "No response"}
            else:
                # Parse ping latency
                match = re.search(r'time=(\d+\.?\d*)', ping_result.stdout)
                latency = float(match.group(1)) if match else 0
                return {"reachable": True, "method": "ping", "latency_ms": latency}
        
        # Parse MTR output
        lines = result.stdout.strip().split('\n')
        hops = []
        final_hop = None
        
        for line in lines[1:]:  # Skip header
            parts = line.split()
            if len(parts) >= 8:
                try:
                    hop_num = int(parts[0].rstrip('.'))
                    host = parts[1]
                    loss = float(parts[2].rstrip('%'))
                    avg = float(parts[5]) if parts[5] != '???' else -1
                    hops.append({"hop": hop_num, "host": host, "loss": loss, "avg": avg})
                    final_hop = {"host": host, "loss": loss, "avg": avg}
                except (ValueError, IndexError):
                    continue
        
        # Check if target is reachable
        reachable = final_hop and final_hop["loss"] < 100 and final_hop["host"] != "???"
        
        # Find problem hop
        problem = None
        for h in hops:
            if h["loss"] > 50 and h["host"] != "???":
                problem = h
                break
        
        return {
            "reachable": reachable,
            "method": "mtr",
            "hops": len(hops),
            "final_loss": final_hop["loss"] if final_hop else 100,
            "final_latency": final_hop["avg"] if final_hop else -1,
            "problem_hop": problem
        }
        
    except subprocess.TimeoutExpired:
        return {"reachable": False, "method": "timeout", "detail": "MTR timed out"}
    except Exception as e:
        return {"reachable": False, "method": "error", "detail": str(e)}

def get_recent_downs(seconds=60):
    """Get workers that went down recently"""
    cutoff = datetime.utcnow() - timedelta(seconds=seconds)
    with lock:
        return [e for e in event_cache if e["type"] == "down" 
                and datetime.fromisoformat(e["ts"]) > cutoff]

def add_event(event_type, worker_id, ip=None, data=None):
    """Add event to cache"""
    event = {
        "ts": datetime.utcnow().isoformat(),
        "type": event_type,
        "worker": worker_id,
        "ip": ip,
        "data": data or {}
    }
    with lock:
        event_cache.append(event)
        # Keep only last 500 events
        if len(event_cache) > 500:
            event_cache[:] = event_cache[-500:]
    return event

# === Watchdog Thread ===
def watchdog_thread():
    """Simple watchdog that checks for missed pings"""
    while True:
        time.sleep(30)  # Check every 30 seconds
        now = datetime.utcnow()
        
        with lock:
            workers_copy = dict(workers)
        
        for worker_id, data in workers_copy.items():
            last_seen = data.get("last_seen")
            if not last_seen:
                continue
            
            seconds_since = (now - last_seen).total_seconds()
            
            if seconds_since > FAIL_TIMEOUT and data.get("online", True):
                # Worker is down
                with lock:
                    if worker_id in workers:
                        workers[worker_id]["online"] = False
                
                worker_ip = data.get("ip")
                
                # Build notification message
                msg = f"🔴 {worker_id} is DOWN"
                
                # Add client-side hop info if available
                hop_data = last_hop_data.get(worker_id, {})
                problem = find_problem_hop(hop_data)
                if problem:
                    msg += f"\n📍 Client→Server: Hop {problem['hop']} ({problem.get('host', '?')}) - {problem.get('loss', 0):.0f}% loss"
                
                # Run server-side MTR to check if we can reach the worker's IP
                server_check = None
                if worker_ip:
                    print(f"[{now}] Running server MTR to {worker_ip}...")
                    server_check = run_server_mtr(worker_ip)
                    
                    if server_check.get("reachable"):
                        msg += f"\n🔍 Server→Client: Reachable ({server_check.get('final_latency', 0):.0f}ms)"
                        msg += "\n→ Issue likely on client side or client→server path"
                    else:
                        msg += f"\n🔍 Server→Client: UNREACHABLE"
                        if server_check.get("problem_hop"):
                            ph = server_check["problem_hop"]
                            msg += f"\n📍 Problem at hop {ph['hop']} ({ph['host']}) - {ph['loss']:.0f}% loss"
                        msg += "\n→ Network issue affects both directions"
                
                # Check if multiple from same IP
                recent = get_recent_downs(60)
                same_ip = [e for e in recent if e.get("ip") == worker_ip]
                if len(same_ip) >= 3:
                    msg += f"\n⚠️ FLEET OUTAGE: {len(same_ip)} workers from same IP down"
                
                add_event("down", worker_id, worker_ip, {
                    "hop_data": hop_data,
                    "server_check": server_check
                })
                message_queue.put(msg)
                print(f"[{now}] {worker_id} DOWN - Server check: {server_check}")

# === Telegram Thread ===
def telegram_sender():
    """Send Telegram notifications"""
    chat_id = os.getenv("CHAT_ID")
    token = os.getenv("TELEGRAM_TOKEN")
    
    if not chat_id or not token:
        print("WARNING: Telegram not configured")
        return
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    while True:
        try:
            msg = message_queue.get()
            resp = requests.post(url, json={"chat_id": chat_id, "text": msg}, timeout=10)
            if not resp.ok:
                print(f"Telegram error: {resp.text}")
        except Exception as e:
            print(f"Telegram error: {e}")

# Start background threads
threading.Thread(target=watchdog_thread, daemon=True).start()
threading.Thread(target=telegram_sender, daemon=True).start()

# === Routes ===
@app.route('/ping/<worker_id>', methods=['GET', 'POST'])
def ping(worker_id):
    """Handle ping from worker"""
    api_key = request.args.get('api_key')
    if api_key != os.getenv("API_KEY"):
        return jsonify({"error": "Invalid API key"}), 403
    
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if client_ip:
        client_ip = client_ip.split(',')[0].strip()
    
    # Get hop data if POST
    hop_data = {}
    if request.method == 'POST':
        try:
            hop_data = request.get_json(silent=True) or {}
        except:
            pass
    
    now = datetime.utcnow()
    
    with lock:
        is_new = worker_id not in workers or not workers.get(worker_id, {}).get("online", False)
        
        workers[worker_id] = {
            "last_seen": now,
            "ip": client_ip,
            "online": True
        }
    
    # Store hop data
    if hop_data:
        last_hop_data[worker_id] = hop_data
        # Log to file (in background to not block)
        threading.Thread(target=log_hop_data, args=(worker_id, hop_data), daemon=True).start()
    
    # Send UP notification for new/recovered workers
    if is_new:
        add_event("up", worker_id, client_ip)
        message_queue.put(f"🟢 {worker_id} is UP")
        print(f"[{now}] {worker_id} UP from {client_ip}")
    
    return jsonify({"status": "ok", "worker": worker_id})

@app.route('/')
def index():
    """Status page"""
    with lock:
        online = {k: v for k, v in workers.items() if v.get("online")}
    
    return jsonify({
        "status": "running",
        "workers_online": len(online),
        "fail_timeout": FAIL_TIMEOUT,
        "workers": {k: v["last_seen"].isoformat() for k, v in online.items()}
    })

@app.route('/api/status')
def api_status():
    """Detailed status"""
    now = datetime.utcnow()
    with lock:
        result = []
        for worker_id, data in workers.items():
            if data.get("online"):
                last = data.get("last_seen", now)
                result.append({
                    "id": worker_id,
                    "ip": data.get("ip"),
                    "last_seen": last.isoformat(),
                    "seconds_ago": (now - last).total_seconds(),
                    "has_hop_data": worker_id in last_hop_data
                })
    
    return jsonify({"online": len(result), "workers": sorted(result, key=lambda x: x["id"])})

@app.route('/api/worker/<worker_id>')
def api_worker(worker_id):
    """Worker details including last hop data"""
    with lock:
        data = workers.get(worker_id)
    
    if not data:
        return jsonify({"error": "Worker not found"}), 404
    
    hop_data = last_hop_data.get(worker_id, {})
    
    return jsonify({
        "id": worker_id,
        "ip": data.get("ip"),
        "online": data.get("online"),
        "last_seen": data.get("last_seen", datetime.utcnow()).isoformat(),
        "hop_data": hop_data
    })

@app.route('/api/events')
@requires_auth
def api_events():
    """Recent events"""
    limit = request.args.get('limit', 100, type=int)
    with lock:
        return jsonify({"events": event_cache[-limit:]})

@app.route('/admin')
@requires_auth
def admin():
    """Simple admin dashboard"""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Uptime Bot Admin</title>
        <style>
            body { font-family: monospace; background: #1a1a2e; color: #eee; padding: 20px; }
            h1 { color: #00d4ff; }
            .worker { background: #16213e; padding: 10px; margin: 5px 0; border-radius: 5px; }
            .online { border-left: 3px solid #00ff88; }
            .event { padding: 5px; margin: 3px 0; }
            .up { color: #00ff88; }
            .down { color: #ff4444; }
            pre { background: #0f0f23; padding: 10px; overflow-x: auto; }
        </style>
    </head>
    <body>
        <h1>🔍 Uptime Bot - RCA Dashboard</h1>
        <div id="status"></div>
        <h2>Recent Events</h2>
        <div id="events"></div>
        <script>
            async function load() {
                const status = await (await fetch('/api/status')).json();
                document.getElementById('status').innerHTML = 
                    `<h2>Workers Online: ${status.online}</h2>` +
                    status.workers.map(w => `
                        <div class="worker online">
                            <strong>${w.id}</strong> (${w.ip}) - ${w.seconds_ago.toFixed(0)}s ago
                            ${w.has_hop_data ? ' 📊' : ''}
                        </div>
                    `).join('');
                
                const events = await (await fetch('/api/events?limit=30')).json();
                document.getElementById('events').innerHTML = 
                    events.events.reverse().map(e => `
                        <div class="event ${e.type}">${e.ts} - ${e.worker} ${e.type.toUpperCase()}</div>
                    `).join('');
            }
            load();
            setInterval(load, 10000);
        </script>
    </body>
    </html>
    """
    return render_template_string(html)

if __name__ == '__main__':
    print(f"Starting Uptime Bot Server (RCA)")
    print(f"  FAIL_TIMEOUT: {FAIL_TIMEOUT}s")
    print(f"  DATA_DIR: {DATA_DIR}")
    app.run(host='0.0.0.0', port=int(os.getenv("SERVER_PORT", 5000)), threaded=True)
