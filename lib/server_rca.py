#!/usr/bin/env python3
"""
Enhanced Uptime Server with RCA (Root Cause Analysis) Support
- Stores all events to JSON file for historical analysis
- Detects patterns (mass failures = network issue)
- Provides API endpoints for querying event history
- Authentication for sensitive endpoints
- Receives MTR/diagnostic data from clients
"""

from flask import jsonify, request, Flask, render_template, Response
from functools import wraps
from queue import Queue
import threading
from threading import Timer, Event
import os
import time
import json
import hashlib
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from utilities import telegram_request

app = Flask(__name__)

# Configuration
FAIL_TIMEOUT = int(os.getenv("FAIL_TIMEOUT", 180))
DATA_DIR = Path(os.getenv("DATA_DIR", "/var/lib/uptime-monitor"))
EVENTS_FILE = DATA_DIR / "events.jsonl"
DIAGNOSTICS_FILE = DATA_DIR / "diagnostics.jsonl"
MAX_EVENTS_FILE_SIZE = 100 * 1024 * 1024  # 100MB

# Authentication - set via environment or generate random
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "")  # Must be set in production!
API_KEY = os.getenv("API_KEY", "")

# Ensure data directory exists
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Core data structures
timers = {}
pause_events = {}
last_seen = {}
worker_diagnostics = {}  # Store latest diagnostics per worker
message_queue = Queue()

# In-memory event cache (last 1000 events)
event_cache = []
MAX_CACHE_SIZE = 1000


# ============ Authentication ============

def check_auth(username, password):
    """Check if username/password is valid"""
    if not ADMIN_PASS:
        return False  # No password set = deny all
    return username == ADMIN_USER and password == ADMIN_PASS


def authenticate():
    """Send 401 response for authentication"""
    return Response(
        'Authentication required.\n'
        'Please login with proper credentials.', 401,
        {'WWW-Authenticate': 'Basic realm="Uptime Monitor Admin"'})


def requires_auth(f):
    """Decorator for routes that require authentication"""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated


def requires_api_key(f):
    """Decorator for API routes that need API key"""
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.args.get('api_key') or request.headers.get('X-API-Key')
        if key != API_KEY:
            return jsonify({"error": "Invalid API key"}), 401
        return f(*args, **kwargs)
    return decorated


# ============ Event Logging ============

def rotate_file(filepath):
    """Rotate file if too large"""
    if filepath.exists() and filepath.stat().st_size > MAX_EVENTS_FILE_SIZE:
        backup = filepath.with_suffix(f".{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl")
        filepath.rename(backup)


def log_event(event_type, worker, client_ip=None, extra=None):
    """Log event to file and cache"""
    event = {
        "ts": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": event_type,
        "worker": worker,
        "client_ip": client_ip,
        **(extra or {})
    }
    
    try:
        rotate_file(EVENTS_FILE)
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as e:
        print(f"Error writing event: {e}")
    
    event_cache.append(event)
    if len(event_cache) > MAX_CACHE_SIZE:
        event_cache.pop(0)
    
    return event


def log_diagnostics(worker, client_ip, diag_data):
    """Log diagnostics data from client"""
    record = {
        "ts": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "worker": worker,
        "client_ip": client_ip,
        **diag_data
    }
    
    try:
        rotate_file(DIAGNOSTICS_FILE)
        with open(DIAGNOSTICS_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        print(f"Error writing diagnostics: {e}")
    
    # Store latest diagnostics per worker
    worker_diagnostics[worker] = record
    
    return record


def get_recent_events(limit=100, event_type=None, worker=None, since_minutes=None):
    """Get recent events from cache"""
    events = event_cache[-limit*2:]
    
    if since_minutes:
        cutoff = datetime.utcnow() - timedelta(minutes=since_minutes)
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        events = [e for e in events if e["ts"] >= cutoff_str]
    
    if event_type:
        events = [e for e in events if e["type"] == event_type]
    
    if worker:
        events = [e for e in events if e["worker"] == worker]
    
    return events[-limit:]


def analyze_failures():
    """Analyze recent failures to determine root cause pattern"""
    recent_downs = get_recent_events(limit=100, event_type="down", since_minutes=5)
    
    if not recent_downs:
        return {
            "status": "healthy",
            "message": "No recent failures",
            "affected_workers": [],
            "likely_cause": None
        }
    
    workers = list(set(e["worker"] for e in recent_downs))
    ips = list(set(e.get("client_ip", "unknown") for e in recent_downs))
    
    if len(workers) >= 5:
        if len(ips) == 1:
            return {
                "status": "network_issue",
                "severity": "high",
                "message": f"NETWORK ISSUE: {len(workers)} workers from same IP went down",
                "affected_workers": workers,
                "likely_cause": "ISP/upstream connectivity issue or local network problem"
            }
        elif len(ips) <= 2:
            return {
                "status": "network_issue",
                "severity": "high", 
                "message": f"REGIONAL ISSUE: {len(workers)} workers from {len(ips)} locations down",
                "affected_workers": workers,
                "likely_cause": "Regional network or upstream provider issue"
            }
        else:
            return {
                "status": "possible_ddos",
                "severity": "critical",
                "message": f"POSSIBLE DDOS: {len(workers)} workers from {len(ips)} IPs down",
                "affected_workers": workers,
                "likely_cause": "DDoS attack on bot server or widespread outage"
            }
    elif len(workers) >= 2:
        return {
            "status": "partial_outage",
            "severity": "medium",
            "message": f"PARTIAL: {len(workers)} workers down recently",
            "affected_workers": workers,
            "likely_cause": "Localized network issue or coincidental failures"
        }
    else:
        return {
            "status": "individual_failure",
            "severity": "low",
            "message": f"Individual failure: {workers[0]}",
            "affected_workers": workers,
            "likely_cause": "Individual server issue (reboot, GPU crash, etc.)"
        }


def message_sender():
    """Background thread to send Telegram messages with rate limiting"""
    while True:
        message = message_queue.get()
        try:
            while True:
                response = telegram_request("/sendMessage?chat_id=" + os.getenv("CHAT_ID") + "&text=" + message)
                if response.get('error_code') == 429:
                    retry_after = response.get('parameters', {}).get('retry_after', 1)
                    time.sleep(retry_after)
                else:
                    break
        except Exception as e:
            print(f"Error sending message: {e}")
        finally:
            message_queue.task_done()


threading.Thread(target=message_sender, daemon=True).start()


def missed_ping(worker):
    """Called when a worker misses its ping timeout"""
    pause_event = pause_events.get(worker)
    if pause_event is not None:
        pause_event.wait()

    current_time = datetime.now()
    last_ping = last_seen.get(worker, current_time)
    
    if (current_time - last_ping) > timedelta(seconds=FAIL_TIMEOUT):
        print(f"[{current_time.strftime('%Y-%m-%d %H:%M:%S')}] DOWN: {worker}")
        
        log_event("down", worker, extra={
            "last_seen": last_ping.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "seconds_since_ping": (current_time - last_ping).total_seconds()
        })
        
        analysis = analyze_failures()
        
        msg = f"🔴 {worker} is DOWN"
        if analysis["status"] in ["network_issue", "possible_ddos"]:
            msg += f"\n⚠️ {analysis['message']}"
        
        message_queue.put(msg)
    else:
        print(f"[{current_time.strftime('%Y-%m-%d %H:%M:%S')}] False alarm: {worker}")

    if worker in timers:
        del timers[worker]
    if worker in pause_events:
        del pause_events[worker]


# ============ Public Endpoints (no auth) ============

@app.route('/ping/<worker_id>', methods=['GET', 'POST'])
def ping(worker_id):
    """Receive heartbeat ping from worker (protected by API key only)"""
    api_key = request.args.get('api_key')
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    if api_key != API_KEY:
        print(f"Invalid API key for {worker_id} from {client_ip}")
        return jsonify({"status": 0, "msg": "Invalid API key"})

    current_time = datetime.now()
    last_seen[worker_id] = current_time
    worker_was_new = worker_id not in timers

    # Cancel existing timer
    if worker_id in timers:
        timers[worker_id].cancel()

    # Create new timer
    pause_event = Event()
    pause_event.set()
    pause_events[worker_id] = pause_event
    timers[worker_id] = Timer(FAIL_TIMEOUT, missed_ping, [worker_id])
    timers[worker_id].daemon = True
    timers[worker_id].start()

    # Handle POST with diagnostics data
    if request.method == 'POST' and request.is_json:
        diag_data = request.get_json()
        if diag_data:
            log_diagnostics(worker_id, client_ip, diag_data)
            print(f"[{current_time.strftime('%Y-%m-%d %H:%M:%S')}] Received diagnostics from {worker_id}")

    if worker_was_new:
        print(f"[{current_time.strftime('%Y-%m-%d %H:%M:%S')}] UP: {worker_id} from {client_ip}")
        log_event("up", worker_id, client_ip)
        message_queue.put(f"🟢 {worker_id} is UP")

    return jsonify({"status": 1, "msg": "Heartbeat received"})


@app.route('/')
def index():
    """Public dashboard - shows only basic info"""
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    active_workers = sorted(list(timers.keys()))
    # Public view: just count, no names
    return f"""<!DOCTYPE html>
<html>
<head><title>Uptime Monitor</title>
<meta http-equiv="refresh" content="30">
<style>
body {{ font-family: sans-serif; max-width: 600px; margin: 50px auto; text-align: center; }}
.status {{ font-size: 48px; margin: 30px; }}
.healthy {{ color: green; }}
.warning {{ color: orange; }}
.count {{ font-size: 24px; color: #666; }}
</style>
</head>
<body>
<h1>🖥️ Uptime Monitor</h1>
<div class="status healthy">✅ Operational</div>
<div class="count">{len(active_workers)} systems monitored</div>
<p style="color:#999">Last check: {current_time}</p>
<p><a href="/admin">Admin Login</a></p>
</body>
</html>"""


# ============ Protected Admin Endpoints ============

@app.route('/admin')
@requires_auth
def admin_dashboard():
    """Protected admin dashboard with full details"""
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    active_workers = sorted(list(timers.keys()))
    analysis = analyze_failures()
    recent = get_recent_events(limit=20)
    
    workers_html = "\n".join(f"<li>{w}</li>" for w in active_workers)
    events_html = "\n".join(
        f"<tr><td>{e['ts']}</td><td>{'🟢' if e['type']=='up' else '🔴'} {e['type']}</td><td>{e['worker']}</td></tr>"
        for e in reversed(recent)
    )
    
    return f"""<!DOCTYPE html>
<html>
<head><title>Uptime Monitor - Admin</title>
<meta http-equiv="refresh" content="30">
<style>
body {{ font-family: sans-serif; max-width: 1200px; margin: 20px auto; padding: 0 20px; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
.card {{ background: #f5f5f5; padding: 20px; border-radius: 8px; }}
.status-healthy {{ color: green; }}
.status-warning {{ color: orange; }}
.status-danger {{ color: red; }}
table {{ width: 100%; border-collapse: collapse; }}
td, th {{ padding: 8px; text-align: left; border-bottom: 1px solid #ddd; }}
ul {{ columns: 3; }}
</style>
</head>
<body>
<h1>🖥️ Uptime Monitor - Admin</h1>
<p>Last refresh: {current_time}</p>

<div class="card">
<h2>System Health</h2>
<p class="status-{analysis['status']}">{analysis['message']}</p>
{f"<p>Likely cause: {analysis['likely_cause']}</p>" if analysis.get('likely_cause') else ""}
</div>

<div class="grid">
<div class="card">
<h2>Active Workers ({len(active_workers)})</h2>
<ul>{workers_html}</ul>
</div>

<div class="card">
<h2>Recent Events</h2>
<table>
<tr><th>Time</th><th>Type</th><th>Worker</th></tr>
{events_html}
</table>
</div>
</div>

<p><a href="/admin/api/rca">Full RCA Report (JSON)</a> | 
<a href="/admin/api/diagnostics">Diagnostics Data</a></p>
</body>
</html>"""


@app.route('/admin/api/status')
@requires_auth
def admin_api_status():
    """Protected API: Current system status"""
    active = sorted(list(timers.keys()))
    analysis = analyze_failures()
    
    return jsonify({
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "active_workers": len(active),
        "workers": active,
        "health": analysis
    })


@app.route('/admin/api/events')
@requires_auth
def admin_api_events():
    """Protected API: Query event history"""
    limit = request.args.get('limit', 100, type=int)
    event_type = request.args.get('type')
    worker = request.args.get('worker')
    since = request.args.get('since_minutes', type=int)
    
    events = get_recent_events(
        limit=min(limit, 1000),
        event_type=event_type,
        worker=worker,
        since_minutes=since
    )
    
    return jsonify({"count": len(events), "events": events})


@app.route('/admin/api/analysis')
@requires_auth
def admin_api_analysis():
    """Protected API: Get failure analysis"""
    return jsonify(analyze_failures())


@app.route('/admin/api/worker/<worker_id>')
@requires_auth
def admin_api_worker(worker_id):
    """Protected API: Get worker status and diagnostics"""
    is_up = worker_id in timers
    last = last_seen.get(worker_id)
    events = get_recent_events(limit=20, worker=worker_id)
    diag = worker_diagnostics.get(worker_id)
    
    return jsonify({
        "worker": worker_id,
        "status": "up" if is_up else "down",
        "last_seen": last.strftime("%Y-%m-%dT%H:%M:%SZ") if last else None,
        "recent_events": events,
        "last_diagnostics": diag
    })


@app.route('/admin/api/rca')
@requires_auth
def admin_api_rca():
    """Protected API: Detailed RCA report"""
    events = get_recent_events(limit=500, since_minutes=60)
    
    up_count = len([e for e in events if e["type"] == "up"])
    down_count = len([e for e in events if e["type"] == "down"])
    
    down_events = [e for e in events if e["type"] == "down"]
    time_buckets = {}
    for e in down_events:
        bucket = e["ts"][:15] + "0:00Z"
        if bucket not in time_buckets:
            time_buckets[bucket] = []
        time_buckets[bucket].append(e["worker"])
    
    mass_failures = [
        {"time": k, "workers": v, "count": len(v)}
        for k, v in time_buckets.items() if len(v) >= 3
    ]
    
    return jsonify({
        "period": "last_60_minutes",
        "total_events": len(events),
        "up_events": up_count,
        "down_events": down_count,
        "mass_failure_windows": mass_failures,
        "current_analysis": analyze_failures()
    })


@app.route('/admin/api/diagnostics')
@requires_auth  
def admin_api_diagnostics():
    """Protected API: Get all worker diagnostics"""
    return jsonify({
        "count": len(worker_diagnostics),
        "diagnostics": worker_diagnostics
    })


# ============ Startup ============

def load_event_cache():
    """Load recent events from file into memory cache"""
    if EVENTS_FILE.exists():
        try:
            with open(EVENTS_FILE, "r") as f:
                lines = f.readlines()[-MAX_CACHE_SIZE:]
                for line in lines:
                    try:
                        event_cache.append(json.loads(line.strip()))
                    except:
                        pass
            print(f"Loaded {len(event_cache)} events into cache")
        except Exception as e:
            print(f"Error loading event cache: {e}")


if __name__ == '__main__':
    print(f"Enhanced Uptime Monitor with RCA Support")
    print(f"FAIL_TIMEOUT: {FAIL_TIMEOUT}s")
    print(f"Data directory: {DATA_DIR}")
    print(f"Admin user: {ADMIN_USER}")
    print(f"Admin password: {'SET' if ADMIN_PASS else 'NOT SET - admin disabled!'}")
    
    load_event_cache()
    
    app.run(host="0.0.0.0", port=int(os.getenv("SERVER_PORT", 5000)))
