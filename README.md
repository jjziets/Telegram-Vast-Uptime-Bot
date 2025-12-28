# Uptime Monitor with RCA (Root Cause Analysis)

Monitor your GPU machines and get Telegram notifications when they go down. Now with **RCA capabilities** to diagnose whether issues are caused by network problems, DDoS, or individual server failures.

## Features

- 🔔 **Telegram Notifications** - Instant alerts when machines go up/down
- 🔒 **Secure Dashboard** - Public page hides details, admin requires login
- 📊 **RCA Analysis** - Detects mass failures (network issue) vs individual failures
- 📡 **MTR Logging** - Captures traceroute data on failures for investigation
- 📋 **Event History** - Stores all events for retrospective analysis
- 🔄 **Backlog on Recovery** - Sends diagnostics when connection recovers

![Dashboard](https://github.com/jjziets/Telegram-Vast-Uptime-Bot/assets/19214485/a3de851e-738c-49cd-852c-bb702e7800f2)

## Quick Start

### 1. Setup Telegram Bot

1. Search for **BotFather** in Telegram
2. Send `/newbot` and follow the prompts
3. Copy the token it gives you
4. Create a group chat, add your bot, and send `/start`

### 2. Server Setup (VPS)

A $2.50-$3.50 server from [Vultr](https://www.vultr.com/?ref=8581277-6G) works great ($100 credit with referral).

```bash
# Install dependencies
sudo apt update && sudo apt install -y git python3 python3-pip

# Clone repository
git clone https://github.com/jjziets/Telegram-Vast-Uptime-Bot.git
cd Telegram-Vast-Uptime-Bot
pip install -r requirements.txt

# Create data directory for RCA
sudo mkdir -p /var/lib/uptime-monitor

# Create .env file
cat > .env << 'EOF'
CHAT_ID=-123456789          # Your Telegram chat ID (see below)
TELEGRAM_TOKEN=your_token   # From BotFather
API_KEY=your_secret_key     # Random string for client auth
SERVER_ADDR=your_server_ip  # Your VPS IP
SERVER_PORT=5000
FAIL_TIMEOUT=180            # Seconds before marking as down
PING_INTERVAL=30            # Client ping interval

# RCA Admin (optional but recommended)
ADMIN_USER=admin
ADMIN_PASS=your_secure_password
DATA_DIR=/var/lib/uptime-monitor
EOF
```

#### Get Chat ID
After adding your bot token to `.env`:
```bash
./run_server.sh chat_id
```

Or for groups: Check URL in Telegram Web - use `-XXXXXXXXX` format (add `-100` prefix for private channels).

#### Start Server

**Basic server** (original):
```bash
./run_server.sh
```

**RCA server** (recommended - with diagnostics):
```bash
cd lib && python3 server_rca.py
```

**Auto-start on boot:**
```bash
# For RCA server:
(crontab -l; echo "@reboot screen -dmS uptime-rca bash -c 'cd /root/Telegram-Vast-Uptime-Bot && source .env && cd lib && python3 server_rca.py'") | crontab -
```

### 3. Client Setup (GPU Machines)

```bash
# Clone repository
git clone https://github.com/jjziets/Telegram-Vast-Uptime-Bot.git
cd Telegram-Vast-Uptime-Bot

# Create .env (only needs these)
cat > .env << 'EOF'
API_KEY=your_secret_key     # Same as server
SERVER_ADDR=your_server_ip  # Your VPS IP
SERVER_PORT=5000
PING_INTERVAL=30
FAIL_TIMEOUT=30             # Curl timeout
EOF

# Install mtr for diagnostics (RCA client only)
sudo apt install -y mtr curl jq
```

#### Start Client

**Basic client:**
```bash
./run_client.sh $(hostname)
```

**RCA client** (recommended - with diagnostics):
```bash
./run_client_rca.sh $(hostname)
```

**Auto-start on boot:**
```bash
# For RCA client:
(crontab -l; echo "@reboot screen -dmS uptime-rca /root/Telegram-Vast-Uptime-Bot/run_client_rca.sh \$(hostname)") | crontab -
```

## Dashboards & API

### Public Dashboard
`http://your-server:5000/` - Shows only "X systems monitored" (safe to expose)

### Admin Dashboard  
`http://your-server:5000/admin` - Full details (requires login)

### RCA API Endpoints
All require authentication (`-u admin:password`):

```bash
# Current status with all workers
curl -u admin:pass http://server:5000/admin/api/status

# Recent events (up/down)
curl -u admin:pass "http://server:5000/admin/api/events?limit=50"

# Failure analysis (is it network issue or individual?)
curl -u admin:pass http://server:5000/admin/api/analysis

# Full RCA report
curl -u admin:pass http://server:5000/admin/api/rca

# Specific worker with MTR data
curl -u admin:pass http://server:5000/admin/api/worker/brickbox-43(4)

# All client diagnostics
curl -u admin:pass http://server:5000/admin/api/diagnostics
```

## RCA: Investigating Outages

When you get down/up notifications, the RCA features help determine the cause:

### Automatic Detection

The server analyzes failure patterns:
- **5+ workers down from same IP** → Network/ISP issue
- **5+ workers down from different IPs** → Possible DDoS
- **1-2 workers down** → Individual server issue

Telegram notifications include this context:
```
🔴 brickbox-43(4) is DOWN
⚠️ NETWORK ISSUE: 5 workers from same IP went down
```

### Client Diagnostics

The RCA client (`run_client_rca.sh`) captures on each failure:
- Gateway connectivity
- External connectivity (8.8.8.8)
- Bot server ping + packet loss
- Full MTR/traceroute
- DNS resolution time
- TCP port check

Logs stored in `logs/` directory:
- `metrics.jsonl` - All ping attempts
- `diagnostics.jsonl` - Network diagnostics on failures
- `mtr/` - Individual MTR captures

### Offline Analysis

```bash
# Copy logs from a client
scp user@gpu-machine:/path/logs/*.jsonl ./

# Analyze
python3 analyze_rca.py metrics.jsonl diagnostics.jsonl
```

## Ansible Deployment (Multiple Machines)

For deploying to many machines at once:

```bash
# On your control machine (e.g., bbmaint)
cd ansible/

# Copy and edit inventory
cp inventory.example.ini inventory.ini
# Edit with your machine IPs

# Deploy clients to all machines
ansible-playbook -i inventory.ini deploy_rca_client.yml
```

## Configuration Reference

### Server `.env`
```bash
CHAT_ID=-123456789          # Telegram chat/group ID
TELEGRAM_TOKEN=xxx          # Bot token from BotFather
API_KEY=secret              # Shared secret with clients
SERVER_PORT=5000            # Listen port
FAIL_TIMEOUT=180            # Seconds before DOWN notification
ADMIN_USER=admin            # Admin login (RCA server)
ADMIN_PASS=password         # Admin password (RCA server)
DATA_DIR=/var/lib/uptime-monitor  # Where to store events
```

### Client `.env`
```bash
API_KEY=secret              # Same as server
SERVER_ADDR=1.2.3.4         # Server IP/hostname
SERVER_PORT=5000            # Server port
PING_INTERVAL=30            # Seconds between pings
FAIL_TIMEOUT=30             # Curl timeout
```

## Troubleshooting

**Too many false notifications?**
- Increase `FAIL_TIMEOUT` on server (e.g., 300 for 5 minutes)

**Client not sending pings?**
- Check `nvidia-smi` works (client needs GPU count)
- Check network connectivity to server

**Admin login not working?**
- Ensure `ADMIN_PASS` is set in `.env`
- Use RCA server (`server_rca.py`), not basic server

**View server logs:**
```bash
screen -r uptime-rca
# Or check: /tmp/rca_server.log
```

## Credits

Based on [leona/vast.ai-tools](https://github.com/leona/vast.ai-tools)
