# Telegram Uptime Bot - RCA Enhanced Version

Enhanced version with **Root Cause Analysis (RCA)** capabilities to diagnose network issues, DDoS, and server failures.

## Security

- **Public dashboard** (`/`) shows only "X systems monitored" - no details
- **Admin dashboard** (`/admin`) requires authentication
- **API endpoints** (`/admin/api/*`) require authentication
- Set `ADMIN_USER` and `ADMIN_PASS` in `.env` to enable admin access

## New Features

### Client Side (`run_client_rca.sh`)
- **Metrics logging** - Every ping attempt logged to `logs/metrics.jsonl`
- **Network diagnostics** - On failure, captures:
  - Gateway connectivity & latency
  - External connectivity (8.8.8.8)
  - Bot server ping stats
  - Traceroute to bot server
  - DNS resolution time
  - TCP port check
- **Automatic cause detection** - Prints likely cause on failure
- **Log rotation** - Prevents disk fill

### Server Side (`lib/server_rca.py`)
- **Event storage** - All up/down events saved to `/var/lib/uptime-monitor/events.jsonl`
- **Pattern detection** - Detects mass failures (network issue) vs individual failures
- **RCA API endpoints**:
  - `GET /api/status` - Current system status
  - `GET /api/events?limit=100&type=down&since_minutes=60` - Query events
  - `GET /api/analysis` - Failure pattern analysis
  - `GET /api/worker/<id>` - Worker-specific status
  - `GET /api/rca` - Detailed RCA report

### Analyzer Tool (`analyze_rca.py`)
- Offline analysis of metrics and diagnostics logs
- Identifies root cause patterns
- Detects mass failures

## Installation

### Server (159.65.255.55)

```bash
# Copy new server
scp lib/server_rca.py root@159.65.255.55:/root/Telegram-Vast-Uptime-Bot/lib/

# Create data directory
ssh root@159.65.255.55 "mkdir -p /var/lib/uptime-monitor"

# Start enhanced server (in screen)
ssh root@159.65.255.55
screen -r  # or create new screen
cd /root/Telegram-Vast-Uptime-Bot
source .env
cd lib && python3 server_rca.py
```

### Clients (BrickBoxes)

```bash
# Copy to each brickbox
scp run_client_rca.sh root@88.0.33.141:/root/vast-uptime_monitor/
scp run_client_rca.sh root@88.0.42.1:/root/vast-uptime_monitor/
# ... etc

# On each brickbox, restart the client
screen -r uptime-client
# Ctrl+C to stop old client
./run_client_rca.sh $(hostname)
```

## Querying RCA Data

### Via API
```bash
# Current status
curl http://159.65.255.55:5000/api/status | jq

# Recent down events
curl "http://159.65.255.55:5000/api/events?type=down&limit=50" | jq

# Failure analysis
curl http://159.65.255.55:5000/api/analysis | jq

# Full RCA report
curl http://159.65.255.55:5000/api/rca | jq
```

### Offline Analysis
```bash
# Copy logs from a brickbox
scp root@88.0.43.1:/root/vast-uptime_monitor/logs/*.jsonl ./

# Analyze
python3 analyze_rca.py metrics.jsonl diagnostics.jsonl
```

## Interpreting Results

### Failure Patterns

| Pattern | Likely Cause |
|---------|--------------|
| Many workers down simultaneously | Network/ISP issue or DDoS |
| Single worker down | Individual server issue (GPU crash, reboot) |
| Gateway failures in diagnostics | Local network problem at colo |
| External (8.8.8.8) failures | ISP/upstream provider issue |
| Bot server unreachable | Bot server down or path blocked |
| DNS failures | DNS server or network config issue |

### Example RCA Session

```bash
# 1. Check server analysis
curl http://159.65.255.55:5000/api/analysis
# Returns: {"status": "network_issue", "message": "5 workers from same IP down"}

# 2. Check specific worker
curl http://159.65.255.55:5000/api/worker/brickbox-43(4)

# 3. Get logs from affected worker
ssh root@88.0.43.1 "tail -100 /root/vast-uptime_monitor/logs/diagnostics.jsonl" | \
  python3 -c "import sys,json; [print(json.loads(l)) for l in sys.stdin]"

# 4. Analyze patterns
python3 analyze_rca.py --events server_events.jsonl
```

## Log Formats

### metrics.jsonl (Client)
```json
{"ts":"2024-12-28T15:30:00Z","worker":"brickbox-43(4)","status":"fail","http":"0","latency_ms":30000,"error":"timeout","consecutive_failures":3}
```

### diagnostics.jsonl (Client)
```json
{"ts":"2024-12-28T15:30:30Z","worker":"brickbox-43(4)","type":"network_diag","gateway":"88.0.88.88","gw_loss_pct":0,"gw_avg_ms":0.2,"external_loss_pct":0,"external_avg_ms":5.5,"bot_loss_pct":100,"bot_avg_ms":-1,"traceroute":"1:88.0.88.88,2:65.100.249.216,...","dns_ok":1,"dns_ms":15,"tcp_port_ok":0}
```

### events.jsonl (Server)
```json
{"ts":"2024-12-28T15:30:00Z","type":"down","worker":"brickbox-43(4)","client_ip":"216.249.100.66","last_seen":"2024-12-28T15:26:30Z","seconds_since_ping":210}
```

