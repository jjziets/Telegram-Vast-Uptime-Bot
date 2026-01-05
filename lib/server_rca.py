#!/usr/bin/env python3
"""
Enhanced Uptime Bot Server with MTR/Hop Logging
- Receives hop data with each ping from clients
- Stores historical hop data for RCA analysis
- When workers go down, shows which hop is the problem
"""

import os
import json
import threading
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, jsonify, request, render_template_string, Response
from queue import Queue
from collections import defaultdict
import requests
import time

app = Flask(__name__)

# Configuration
FAIL_TIMEOUT = int(os.getenv("FAIL_TIMEOUT", 180))
DATA_DIR = os.getenv("DATA_DIR", "/var/lib/uptime-bot")
MAX_HOP_HISTORY = 1000  # Keep last N hop records per worker

# Storage
worker_timers = {}          # worker_id -> threading.Timer
worker_last_seen = {}       # worker_id -> datetime
worker_ip = {}              # worker_id -> source IP
worker_hop_history = defaultdict(list)  # worker_id -> [{ts, hops, loss, avg}]
event_log = []              # [{ts, type, worker, data}]
message_queue = Queue()
data_lock = threading.Lock()

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

# === Event Logging ===

def log_event(event_type, worker_id, data=None):
    """Log an event for historical analysis"""
    with data_lock:
        event = {
            "ts": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "type": event_type,
            "worker": worker_id,
            "ip": worker_ip.get(worker_id),
            "data": data or {}
        }
        event_log.append(event)
        
        # Trim log if too large
        if len(event_log) > 10000:
            event_log[:] = event_log[-5000:]
        
        # Persist to file
        try:
            with open(os.path.join(DATA_DIR, "events.jsonl"), "a") as f:
                f.write(json.dumps(event) + "\n")
        except Exception as e:
            print(f"Error writing event: {e}")

def store_hop_data(worker_id, hop_data):
    """Store hop data for a worker"""
    with data_lock:
        worker_hop_history[worker_id].append(hop_data)
        # Trim old data
        if len(worker_hop_history[worker_id]) > MAX_HOP_HISTORY:
            worker_hop_history[worker_id] = worker_hop_history[worker_id][-MAX_HOP_HISTORY:]
    
    # Persist to file
    try:
        with open(os.path.join(DATA_DIR, "hops.jsonl"), "a") as f:
            f.write(json.dumps({"worker": worker_id, **hop_data}) + "\n")
    except Exception as e:
        print(f"Error writing hop data: {e}")

def find_problem_hop(hop_data):
    """Analyze hop data to find the problem hop"""
    hops = hop_data.get("hops", [])
    if not hops:
        return None
    
    # Find first hop with significant loss
    for hop in hops:
        if hop.get("loss", 0) > 50 or hop.get("host") == "timeout":
            return hop
    
    # Find hop with highest loss
    worst = max(hops, key=lambda h: h.get("loss", 0), default=None)
    if worst and worst.get("loss", 0) > 10:
        return worst
    
    return None

def get_last_hop_data(worker_id, count=5):
    """Get recent hop data for a worker"""
    with data_lock:
        return worker_hop_history.get(worker_id, [])[-count:]

def analyze_worker_outage(worker_id):
    """Analyze why a worker might be down based on hop history"""
    recent = get_last_hop_data(worker_id, 10)
    if not recent:
        return {"status": "no_data", "message": "No hop data available"}
    
    # Find problem hops across recent data
    problem_hops = []
    for data in recent:
        problem = find_problem_hop(data)
        if problem:
            problem_hops.append(problem)
    
    if not problem_hops:
        return {"status": "unknown", "message": "No obvious network issue detected"}
    
    # Count which hop appears most often as problem
    hop_counts = defaultdict(int)
    hop_info = {}
    for h in problem_hops:
        hop_counts[h["hop"]] += 1
        hop_info[h["hop"]] = h
    
    most_common = max(hop_counts.items(), key=lambda x: x[1])
    problem = hop_info[most_common[0]]
    
    return {
        "status": "problem_identified",
        "hop_number": problem["hop"],
        "hop_host": problem.get("host", "unknown"),
        "loss_pct": problem.get("loss", 0),
        "frequency": f"{most_common[1]}/{len(recent)}",
        "message": f"Hop {problem['hop']} ({problem.get('host', '?')}) showing {problem.get('loss', 0):.0f}% loss"
    }

def analyze_fleet_outage():
    """Analyze if multiple workers from same IP are down"""
    with data_lock:
        # Find workers that recently went down
        recent_downs = [e for e in event_log[-100:] if e["type"] == "down"]
        if len(recent_downs) < 3:
            return None
        
        # Check if from same IP in last 60 seconds
        cutoff = datetime.utcnow() - timedelta(seconds=60)
        recent = [e for e in recent_downs 
                  if datetime.strptime(e["ts"], "%Y-%m-%dT%H:%M:%SZ") > cutoff]
        
        # Group by IP
        by_ip = defaultdict(list)
        for e in recent:
            ip = e.get("ip")
            if ip:
                by_ip[ip].append(e["worker"])
        
        # Find IP with most downs
        if by_ip:
            worst_ip = max(by_ip.items(), key=lambda x: len(x[1]))
            if len(worst_ip[1]) >= 3:
                return {
                    "ip": worst_ip[0],
                    "workers": worst_ip[1],
                    "count": len(worst_ip[1]),
                    "message": f"NETWORK ISSUE: {len(worst_ip[1])} workers from {worst_ip[0]} went down"
                }
    return None

# === Timer/Notification Logic ===

def missed_ping(worker):
    """Called when a worker misses its ping deadline"""
    with data_lock:
        current_time = datetime.now()
        last_ping = worker_last_seen.get(worker)
        
        if last_ping and (current_time - last_ping) > timedelta(seconds=FAIL_TIMEOUT):
            print(f"[{current_time}] Worker {worker} DOWN")
            
            # Analyze why
            outage_info = analyze_worker_outage(worker)
            fleet_info = analyze_fleet_outage()
            
            # Build message
            msg = f"🔴 {worker} is DOWN"
            
            # Add hop info if available
            if outage_info["status"] == "problem_identified":
                msg += f"\n📍 {outage_info['message']}"
            
            # Add fleet info if multiple down
            if fleet_info:
                msg += f"\n⚠️ {fleet_info['message']}"
            
            # Log event with full details
            log_event("down", worker, {
                "last_seen": last_ping.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "outage_analysis": outage_info,
                "fleet_analysis": fleet_info
            })
            
            message_queue.put(msg)
            
            # Remove from tracking
            del worker_timers[worker]

def restart_timer(worker):
    """Restart the timeout timer for a worker"""
    if worker in worker_timers:
        worker_timers[worker].cancel()
    
    timer = threading.Timer(FAIL_TIMEOUT, missed_ping, [worker])
    timer.start()
    worker_timers[worker] = timer

# === Telegram Thread ===

def telegram_sender():
    """Background thread that sends Telegram messages"""
    chat_id = os.getenv("CHAT_ID")
    token = os.getenv("TELEGRAM_TOKEN")
    
    if not chat_id or not token:
        print("WARNING: Telegram credentials not configured!")
        return
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    while True:
        try:
            msg = message_queue.get()
            print(f"Sending Telegram: {msg}")
            
            resp = requests.post(url, json={
                "chat_id": chat_id,
                "text": msg,
                "parse_mode": "Markdown"
            }, timeout=10)
            
            if not resp.ok:
                print(f"Telegram error: {resp.text}")
        except Exception as e:
            print(f"Telegram send error: {e}")

# Start telegram thread
telegram_thread = threading.Thread(target=telegram_sender, daemon=True)
telegram_thread.start()

# === API Routes ===

@app.route('/ping/<worker_id>', methods=['GET', 'POST'])
def ping(worker_id):
    """Handle ping from worker - accepts hop data in POST body"""
    # Verify API key
    api_key = request.args.get('api_key')
    if api_key != os.getenv("API_KEY"):
        return jsonify({"error": "Invalid API key"}), 403
    
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    client_ip = client_ip.split(',')[0].strip() if client_ip else request.remote_addr
    
    # Parse hop data from POST body
    hop_data = {}
    if request.method == 'POST':
        try:
            hop_data = request.get_json(silent=True) or {}
        except:
            pass
    
    with data_lock:
        is_new = worker_id not in worker_timers
        worker_last_seen[worker_id] = datetime.now()
        worker_ip[worker_id] = client_ip
    
    # Store hop data
    if hop_data:
        hop_data["client_ip"] = client_ip
        store_hop_data(worker_id, hop_data)
    
    # Send UP notification if new
    if is_new:
        log_event("up", worker_id, {"ip": client_ip})
        message_queue.put(f"🟢 {worker_id} is UP")
        print(f"[{datetime.now()}] Worker {worker_id} UP from {client_ip}")
    
    restart_timer(worker_id)
    
    return jsonify({
        "status": "ok",
        "worker": worker_id,
        "fail_timeout": FAIL_TIMEOUT
    })

@app.route('/')
def index():
    """Simple status page"""
    with data_lock:
        workers = list(worker_timers.keys())
        status = {w: worker_last_seen.get(w, datetime.min).strftime("%Y-%m-%d %H:%M:%S") 
                  for w in workers}
    
    return jsonify({
        "status": "running",
        "workers_online": len(workers),
        "fail_timeout": FAIL_TIMEOUT,
        "workers": status
    })

@app.route('/api/status')
def api_status():
    """Detailed status API"""
    with data_lock:
        workers = []
        now = datetime.now()
        for w, timer in worker_timers.items():
            last = worker_last_seen.get(w, datetime.min)
            hop_history = worker_hop_history.get(w, [])
            last_hop = hop_history[-1] if hop_history else None
            
            workers.append({
                "id": w,
                "ip": worker_ip.get(w),
                "last_seen": last.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "seconds_ago": (now - last).total_seconds(),
                "hop_records": len(hop_history),
                "last_hop": last_hop
            })
        
        return jsonify({
            "online": len(workers),
            "fail_timeout": FAIL_TIMEOUT,
            "workers": sorted(workers, key=lambda x: x["id"])
        })

@app.route('/api/worker/<worker_id>')
def api_worker(worker_id):
    """Get details for a specific worker including hop history"""
    with data_lock:
        if worker_id not in worker_timers:
            return jsonify({"error": "Worker not found"}), 404
        
        last = worker_last_seen.get(worker_id, datetime.min)
        hop_history = worker_hop_history.get(worker_id, [])
        
        return jsonify({
            "id": worker_id,
            "ip": worker_ip.get(worker_id),
            "last_seen": last.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hop_records": len(hop_history),
            "recent_hops": hop_history[-20:]  # Last 20 entries
        })

@app.route('/api/hops/<worker_id>')
@requires_auth
def api_hops(worker_id):
    """Get full hop history for analysis (protected)"""
    with data_lock:
        hop_history = worker_hop_history.get(worker_id, [])
        return jsonify({
            "worker": worker_id,
            "count": len(hop_history),
            "hops": hop_history
        })

@app.route('/api/events')
@requires_auth
def api_events():
    """Get recent events (protected)"""
    limit = request.args.get('limit', 100, type=int)
    event_type = request.args.get('type')
    
    with data_lock:
        events = event_log[-limit:]
        if event_type:
            events = [e for e in events if e["type"] == event_type]
    
    return jsonify({"events": events})

@app.route('/api/rca/<worker_id>')
@requires_auth
def api_rca(worker_id):
    """Get RCA analysis for a worker (protected)"""
    analysis = analyze_worker_outage(worker_id)
    recent_hops = get_last_hop_data(worker_id, 10)
    
    # Find consistent problem hops
    hop_issues = defaultdict(list)
    for data in recent_hops:
        for hop in data.get("hops", []):
            if hop.get("loss", 0) > 0:
                hop_issues[hop["hop"]].append({
                    "ts": data.get("ts"),
                    "host": hop.get("host"),
                    "loss": hop.get("loss"),
                    "avg": hop.get("avg")
                })
    
    return jsonify({
        "worker": worker_id,
        "analysis": analysis,
        "hop_issues": dict(hop_issues),
        "recent_data_count": len(recent_hops)
    })

# === Admin Dashboard ===

ADMIN_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Uptime Bot - RCA Dashboard</title>
    <style>
        body { font-family: 'JetBrains Mono', monospace; background: #0d1117; color: #c9d1d9; padding: 20px; }
        h1, h2 { color: #58a6ff; }
        .worker { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 15px; margin: 10px 0; }
        .online { border-left: 4px solid #3fb950; }
        .hop-table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        .hop-table th, .hop-table td { padding: 8px; text-align: left; border-bottom: 1px solid #30363d; }
        .loss-high { color: #f85149; }
        .loss-med { color: #d29922; }
        .loss-ok { color: #3fb950; }
        .event { padding: 5px 10px; margin: 5px 0; border-radius: 4px; }
        .event-up { background: #238636; }
        .event-down { background: #da3633; }
        .tabs { display: flex; gap: 10px; margin-bottom: 20px; }
        .tab { padding: 10px 20px; background: #21262d; border-radius: 6px; cursor: pointer; }
        .tab.active { background: #388bfd; }
        .panel { display: none; }
        .panel.active { display: block; }
        pre { background: #161b22; padding: 10px; border-radius: 4px; overflow-x: auto; }
    </style>
</head>
<body>
    <h1>🔍 Uptime Bot - RCA Dashboard</h1>
    
    <div class="tabs">
        <div class="tab active" onclick="showPanel('status')">Status</div>
        <div class="tab" onclick="showPanel('hops')">Hop Data</div>
        <div class="tab" onclick="showPanel('events')">Events</div>
    </div>
    
    <div id="status" class="panel active">
        <h2>Workers Online: <span id="count">-</span></h2>
        <div id="workers"></div>
    </div>
    
    <div id="hops" class="panel">
        <h2>Recent Hop Data</h2>
        <select id="worker-select" onchange="loadWorkerHops()"></select>
        <div id="hop-data"></div>
    </div>
    
    <div id="events" class="panel">
        <h2>Recent Events</h2>
        <div id="event-list"></div>
    </div>
    
    <script>
        function showPanel(name) {
            document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.getElementById(name).classList.add('active');
            event.target.classList.add('active');
        }
        
        function lossClass(loss) {
            if (loss > 50) return 'loss-high';
            if (loss > 10) return 'loss-med';
            return 'loss-ok';
        }
        
        async function loadStatus() {
            const resp = await fetch('/api/status');
            const data = await resp.json();
            document.getElementById('count').textContent = data.online;
            
            const select = document.getElementById('worker-select');
            select.innerHTML = data.workers.map(w => `<option value="${w.id}">${w.id}</option>`).join('');
            
            document.getElementById('workers').innerHTML = data.workers.map(w => `
                <div class="worker online">
                    <strong>${w.id}</strong> (${w.ip})
                    <br>Last seen: ${w.seconds_ago.toFixed(0)}s ago
                    <br>Hop records: ${w.hop_records}
                    ${w.last_hop ? `<br>Last hop data: ${JSON.stringify(w.last_hop.loss || 'N/A')}% loss` : ''}
                </div>
            `).join('');
        }
        
        async function loadWorkerHops() {
            const worker = document.getElementById('worker-select').value;
            if (!worker) return;
            
            const resp = await fetch(`/api/worker/${worker}`);
            const data = await resp.json();
            
            let html = `<h3>${worker} - ${data.hop_records} records</h3>`;
            if (data.recent_hops && data.recent_hops.length > 0) {
                for (const entry of data.recent_hops.slice(-5).reverse()) {
                    html += `<div style="margin: 10px 0"><strong>${entry.ts}</strong>`;
                    if (entry.hops) {
                        html += '<table class="hop-table"><tr><th>Hop</th><th>Host</th><th>Loss</th><th>Avg</th></tr>';
                        for (const h of entry.hops) {
                            html += `<tr>
                                <td>${h.hop}</td>
                                <td>${h.host}</td>
                                <td class="${lossClass(h.loss)}">${h.loss}%</td>
                                <td>${h.avg}ms</td>
                            </tr>`;
                        }
                        html += '</table>';
                    }
                    html += '</div>';
                }
            }
            document.getElementById('hop-data').innerHTML = html;
        }
        
        async function loadEvents() {
            const resp = await fetch('/api/events?limit=50');
            const data = await resp.json();
            
            document.getElementById('event-list').innerHTML = data.events.reverse().map(e => `
                <div class="event event-${e.type}">
                    ${e.ts} - <strong>${e.worker}</strong> ${e.type.toUpperCase()}
                    ${e.ip ? `(${e.ip})` : ''}
                    ${e.data?.outage_analysis?.message ? `<br>→ ${e.data.outage_analysis.message}` : ''}
                </div>
            `).join('');
        }
        
        loadStatus();
        loadEvents();
        setInterval(loadStatus, 10000);
        setInterval(loadEvents, 30000);
    </script>
</body>
</html>
"""

@app.route('/admin')
@requires_auth
def admin_dashboard():
    return render_template_string(ADMIN_TEMPLATE)

# === Main ===

if __name__ == '__main__':
    print(f"Starting Uptime Bot Server with RCA")
    print(f"  FAIL_TIMEOUT: {FAIL_TIMEOUT}s")
    print(f"  DATA_DIR: {DATA_DIR}")
    
    app.run(host='0.0.0.0', port=int(os.getenv("SERVER_PORT", 5000)))
