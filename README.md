# Telegram Vast Uptime Bot - Enhanced with MTR Logging

A Telegram bot that monitors worker/server uptime with **continuous MTR/traceroute logging** for root cause analysis.

## Features

- ✅ Telegram notifications when workers go up/down
- ✅ **Continuous MTR logging** - Every ping includes hop data
- ✅ **Problem hop detection** - Shows which network hop is causing issues
- ✅ **Fleet outage detection** - Identifies when multiple workers from same IP go down
- ✅ Admin dashboard with authentication for viewing hop history
- ✅ Historical event and hop data storage for RCA

## How It Works

### Client Side
The client runs MTR to the server periodically and sends hop data with each ping:
- Every 5th ping: Full MTR trace (all hops with loss/latency)
- Other pings: Quick latency check
- On failure: Full MTR + detailed diagnostics

### Server Side
The server stores hop data and analyzes patterns:
- When a worker goes DOWN, it checks the hop history to identify the problem hop
- Telegram notifications include the specific hop causing issues
- Admin dashboard shows historical hop data for any worker

### Example Notification

```
🔴 brickbox-5(3) is DOWN
📍 Hop 7 (196.60.8.129) showing 78% loss
⚠️ NETWORK ISSUE: 12 workers from 88.0.33.1 went down
```

## Quick Start

### Server Setup

1. Clone the repo:
```bash
git clone https://github.com/jjziets/Telegram-Vast-Uptime-Bot.git
cd Telegram-Vast-Uptime-Bot
```

2. Create `.env`:
```bash
TELEGRAM_TOKEN=your_bot_token
CHAT_ID=your_chat_id
API_KEY=your_secret_api_key
SERVER_PORT=5000
FAIL_TIMEOUT=180
ADMIN_USER=admin
ADMIN_PASS=your_secure_password
DATA_DIR=/var/lib/uptime-bot
```

3. Start the server:
```bash
screen -dmS uptime-server bash -c "source .env && python3 lib/server_rca.py"
```

### Client Setup

1. Copy client files to your worker:
```bash
scp run_client_rca.sh .env user@worker:/opt/uptime-client/
```

2. Create `.env` on client:
```bash
SERVER_ADDR=your-server-ip
SERVER_PORT=5000
API_KEY=your_secret_api_key
PING_INTERVAL=30
FAIL_TIMEOUT=30
```

3. Start the client:
```bash
screen -dmS uptime-client ./run_client_rca.sh my-worker-name
```

4. Add to crontab for boot:
```bash
@reboot screen -dmS uptime-client /opt/uptime-client/run_client_rca.sh my-worker
```

## Admin Dashboard

Access the dashboard at `http://your-server:5000/admin` (requires authentication).

Features:
- View all online workers and their hop history
- See recent events (UP/DOWN)
- Analyze hop data for any worker
- Identify problem hops across the fleet

## API Endpoints

### Public
- `GET /` - Basic status
- `POST /ping/<worker_id>?api_key=xxx` - Worker ping with hop data

### Authenticated (Basic Auth)
- `GET /admin` - Dashboard
- `GET /api/status` - Detailed status
- `GET /api/worker/<id>` - Worker details + recent hops
- `GET /api/hops/<id>` - Full hop history
- `GET /api/events?limit=N` - Recent events
- `GET /api/rca/<id>` - Root cause analysis for a worker

## Understanding the Hop Data

When a worker goes down, check the hop data to identify the problem:

| Hop | Host | Loss | Meaning |
|-----|------|------|---------|
| 1 | 192.168.1.1 | 0% | Local gateway - OK |
| 2 | ISP-router | 0% | ISP edge - OK |
| 7 | **196.60.8.1** | **78%** | **Problem hop!** |
| 8+ | ??? | 100% | Blocked by hop 7 |

Common patterns:
- **Hop 1-2 high loss**: Local network issue
- **Mid-path hop high loss**: ISP/transit issue  
- **Final hop high loss**: Server issue
- **All hops 100% loss**: Complete network outage

## Ansible Deployment

For deploying to multiple workers, see `ansible/` directory:

```bash
cd ansible
cp inventory.example.ini inventory.ini
# Edit inventory.ini with your hosts
ansible-playbook -i inventory.ini deploy_rca_client.yml
```

## Files

- `lib/server_rca.py` - Enhanced server with MTR storage
- `run_client_rca.sh` - Enhanced client with MTR logging
- `ansible/` - Deployment automation
- `logs/` - Local log directory (client-side)

## Environment Variables

### Server
| Variable | Description | Default |
|----------|-------------|---------|
| TELEGRAM_TOKEN | Bot token from @BotFather | required |
| CHAT_ID | Telegram chat ID | required |
| API_KEY | Shared secret for API auth | required |
| SERVER_PORT | HTTP port | 5000 |
| FAIL_TIMEOUT | Seconds before worker marked DOWN | 180 |
| ADMIN_USER | Dashboard username | admin |
| ADMIN_PASS | Dashboard password | admin |
| DATA_DIR | Directory for data files | /var/lib/uptime-bot |

### Client
| Variable | Description | Default |
|----------|-------------|---------|
| SERVER_ADDR | Server hostname/IP | required |
| SERVER_PORT | Server port | 5000 |
| API_KEY | Shared secret | required |
| PING_INTERVAL | Seconds between pings | 30 |

## Troubleshooting

### Client shows "mtr/traceroute failed"
Install mtr: `apt install mtr-tiny` or use fallback traceroute.

### High CPU on client
MTR runs every 5th ping. Increase `PING_INTERVAL` if needed.

### No hop data in notifications
Ensure client is using `run_client_rca.sh`, not the old `run_client.sh`.

## License

MIT
