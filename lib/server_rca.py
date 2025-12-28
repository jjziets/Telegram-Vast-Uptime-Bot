#!/usr/bin/env python3
"""
Enhanced Uptime Server with RCA (Root Cause Analysis) Support
- Stores all events to JSON file for historical analysis
- Detects patterns (mass failures = network issue)
- Provides API endpoints for querying event history
"""

from flask import jsonify, request, Flask, render_template
from queue import Queue
import threading
from threading import Timer, Event
import os
import time
import json
from datetime import datetime, timedelta
from pathlib import Path
from utilities import telegram_request

app = Flask(__name__)

# Configuration
FAIL_TIMEOUT = int(os.getenv("FAIL_TIMEOUT", 180))
DATA_DIR = Path(os.getenv("DATA_DIR", "/var/lib/uptime-monitor"))
EVENTS_FILE = DATA_DIR / "events.jsonl"
MAX_EVENTS_FILE_SIZE = 100 * 1024 * 1024  # 100MB

# Ensure data directory exists
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Core data structures
timers = {}
pause_events = {}
last_seen = {}
message_queue = Queue()

# In-memory event cache (last 1000 events)
event_cache = []
MAX_CACHE_SIZE = 1000


def rotate_events_file():
    """Rotate events file if too large"""
    if EVENTS_FILE.exists() and EVENTS_FILE.stat().st_size > MAX_EVENTS_FILE_SIZE:
        backup = EVENTS_FILE.with_suffix(f".{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl")
        EVENTS_FILE.rename(backup)


def log_event(event_type, worker, client_ip=None, extra=None):
    """Log event to file and cache"""
    event = {
        "ts": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": event_type,
        "worker": worker,
        "client_ip": client_ip,
        **(extra or {})
    }
    
    # Append to file
    try:
        rotate_events_file()
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as e:
        print(f"Error writing event: {e}")
    
    # Add to cache
    event_cache.append(event)
    if len(event_cache) > MAX_CACHE_SIZE:
        event_cache.pop(0)
    
    return event


def get_recent_events(limit=100, event_type=None, worker=None, since_minutes=None):
    """Get recent events from cache"""
    events = event_cache[-limit*2:]  # Get more than needed for filtering
    
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
    # Get failures in last 5 minutes
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


# Start message sender thread
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
        
        # Log event
        log_event("down", worker, extra={
            "last_seen": last_ping.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "seconds_since_ping": (current_time - last_ping).total_seconds()
        })
        
        # Check for mass failure
        analysis = analyze_failures()
        
        # Build message
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


@app.route('/ping/<worker_id>', methods=['GET'])
def ping(worker_id):
    """Receive heartbeat ping from worker"""
    api_key = request.args.get('api_key')
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    if api_key != os.getenv("API_KEY"):
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

    if worker_was_new:
        print(f"[{current_time.strftime('%Y-%m-%d %H:%M:%S')}] UP: {worker_id} from {client_ip}")
        log_event("up", worker_id, client_ip)
        message_queue.put(f"🟢 {worker_id} is UP")

    return jsonify({"status": 1, "msg": "Heartbeat received"})


# ============ RCA API Endpoints ============

@app.route('/api/status')
def api_status():
    """Current system status"""
    active = sorted(list(timers.keys()))
    analysis = analyze_failures()
    
    return jsonify({
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "active_workers": len(active),
        "workers": active,
        "health": analysis
    })


@app.route('/api/events')
def api_events():
    """Query event history"""
    limit = request.args.get('limit', 100, type=int)
    event_type = request.args.get('type')  # up, down
    worker = request.args.get('worker')
    since = request.args.get('since_minutes', type=int)
    
    events = get_recent_events(
        limit=min(limit, 1000),
        event_type=event_type,
        worker=worker,
        since_minutes=since
    )
    
    return jsonify({
        "count": len(events),
        "events": events
    })


@app.route('/api/analysis')
def api_analysis():
    """Get failure analysis"""
    return jsonify(analyze_failures())


@app.route('/api/worker/<worker_id>')
def api_worker(worker_id):
    """Get worker status and recent events"""
    is_up = worker_id in timers
    last = last_seen.get(worker_id)
    events = get_recent_events(limit=20, worker=worker_id)
    
    return jsonify({
        "worker": worker_id,
        "status": "up" if is_up else "down",
        "last_seen": last.strftime("%Y-%m-%dT%H:%M:%SZ") if last else None,
        "recent_events": events
    })


@app.route('/api/rca')
def api_rca():
    """Detailed RCA report"""
    # Get events from last hour
    events = get_recent_events(limit=500, since_minutes=60)
    
    # Count by type
    up_count = len([e for e in events if e["type"] == "up"])
    down_count = len([e for e in events if e["type"] == "down"])
    
    # Group downs by time window (5 min buckets)
    down_events = [e for e in events if e["type"] == "down"]
    time_buckets = {}
    for e in down_events:
        bucket = e["ts"][:15] + "0:00Z"  # Round to 10 min
        if bucket not in time_buckets:
            time_buckets[bucket] = []
        time_buckets[bucket].append(e["worker"])
    
    # Find mass failure windows
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


@app.route('/')
def index():
    """Dashboard"""
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    active_workers = sorted(list(timers.keys()))
    return render_template('index.html', current_time=current_time, active_workers=active_workers)


# Load recent events into cache on startup
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
    print(f"Events file: {EVENTS_FILE}")
    
    load_event_cache()
    
    app.run(host="0.0.0.0", port=int(os.getenv("SERVER_PORT", 5000)))

