#!/bin/bash
# Enhanced Uptime Client with Continuous MTR Logging
# - Runs MTR with every ping and sends data to server
# - Server can analyze hop latency history to identify problem hops
# - On recovery, sends backlog of diagnostics

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source $DIR/.env

WORKER=$1
LOG_DIR="${DIR}/logs"
METRICS_FILE="${LOG_DIR}/metrics.jsonl"
MTR_FILE="${LOG_DIR}/mtr_history.jsonl"
MAX_LOG_SIZE=$((50*1024*1024))  # 50MB max

# Create directories
mkdir -p "$LOG_DIR"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# State
CONSECUTIVE_FAILURES=0
CWORKER=""

if ! [ -n "$WORKER" ]; then
  WORKER="unknown-worker"
fi

# Rotate log if too large
rotate_logs() {
    for f in "$METRICS_FILE" "$MTR_FILE"; do
        if [ -f "$f" ]; then
            local size=$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f" 2>/dev/null || echo 0)
            if [ "$size" -gt $MAX_LOG_SIZE ]; then
                mv "$f" "${f}.$(date +%Y%m%d_%H%M%S).old"
            fi
        fi
    done
}

# Run quick MTR and return JSON with hop data
run_mtr() {
    local ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    
    # Run MTR with 3 pings, report mode
    local mtr_raw=$(mtr -r -c 3 -n "$SERVER_ADDR" 2>/dev/null)
    
    if [ -z "$mtr_raw" ]; then
        # Fallback to traceroute
        mtr_raw=$(traceroute -n -m 15 -w 2 "$SERVER_ADDR" 2>/dev/null)
    fi
    
    if [ -z "$mtr_raw" ]; then
        echo "{\"ts\":\"$ts\",\"error\":\"mtr/traceroute failed\"}"
        return
    fi
    
    # Parse MTR output into JSON array of hops
    # Format: HOST Loss% Snt Last Avg Best Wrst StDev
    local hops=$(echo "$mtr_raw" | tail -n +2 | awk '
    BEGIN { printf "[" }
    NR > 1 { printf "," }
    {
        # Handle MTR format: hop host loss% snt last avg best wrst stdev
        gsub(/%/, "", $3)
        host = $2
        if (host == "???") host = "timeout"
        loss = ($3 == "" || $3 == "???") ? 100 : $3
        avg = ($6 == "" || $6 == "???") ? -1 : $6
        last = ($5 == "" || $5 == "???") ? -1 : $5
        printf "{\"hop\":%d,\"host\":\"%s\",\"loss\":%.1f,\"avg\":%.1f,\"last\":%.1f}", NR, host, loss+0, avg+0, last+0
    }
    END { printf "]" }
    ')
    
    # Also get final destination stats
    local final_loss=$(echo "$mtr_raw" | tail -1 | awk '{gsub(/%/,"",$3); print $3+0}')
    local final_avg=$(echo "$mtr_raw" | tail -1 | awk '{print $6+0}')
    
    echo "{\"ts\":\"$ts\",\"target\":\"$SERVER_ADDR\",\"loss\":${final_loss:-0},\"avg\":${final_avg:-0},\"hops\":$hops}"
}

# Quick ping check (faster than MTR for normal operation)
quick_check() {
    local result=$(ping -c 1 -W 2 "$SERVER_ADDR" 2>&1)
    if echo "$result" | grep -q "1 received"; then
        local latency=$(echo "$result" | grep -oP 'time=\K[0-9.]+')
        echo "${latency:-0}"
        return 0
    else
        echo "-1"
        return 1
    fi
}

# Main ping function with MTR data
do_ping() {
    local ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    
    # Check nvidia-smi
    local driver_check=$(nvidia-smi 2>&1 | grep "Driver Version:")
    if [ $? -eq 0 ]; then
        local numGPUs=$(nvidia-smi --query-gpu=count --format=csv,noheader -i 0 2>/dev/null)
        CWORKER="${WORKER}(${numGPUs})"
    else
        CWORKER="${WORKER}(GPU_ERR)"
    fi
    
    local time_str=$(date "+%H:%M:%S")
    
    # Run MTR in background while we ping
    local mtr_data=""
    
    # Every 5th ping or on failure, run full MTR
    if [ $((RANDOM % 5)) -eq 0 ] || [ $CONSECUTIVE_FAILURES -gt 0 ]; then
        mtr_data=$(run_mtr)
    else
        # Quick ping check for latency
        local ping_latency=$(quick_check)
        mtr_data="{\"ts\":\"$ts\",\"target\":\"$SERVER_ADDR\",\"quick_ping_ms\":$ping_latency}"
    fi
    
    # Build request URL
    local request_url="http://${SERVER_ADDR}:${SERVER_PORT}/ping/${CWORKER}?api_key=${API_KEY}"
    
    # Send ping with MTR data
    local start_ms=$(date +%s%3N)
    local response=$(curl -m ${FAIL_TIMEOUT:-30} -s -w "\n%{http_code}" \
        -X POST \
        -H "Content-Type: application/json" \
        -d "$mtr_data" \
        "$request_url" 2>&1)
    local curl_exit=$?
    local end_ms=$(date +%s%3N)
    local latency=$((end_ms - start_ms))
    
    # Parse response
    local http_code=$(echo "$response" | tail -1)
    local body=$(echo "$response" | head -n -1)
    
    # Log MTR data locally
    echo "$mtr_data" >> "$MTR_FILE"
    
    if [ $curl_exit -eq 0 ] && [ "$http_code" = "200" ]; then
        # Success
        if [ $CONSECUTIVE_FAILURES -gt 0 ]; then
            echo -e "${GREEN}$time_str - RECOVERED${NC} after $CONSECUTIVE_FAILURES failures | ${latency}ms"
        else
            # Extract hop info for display
            local hop_info=""
            if echo "$mtr_data" | grep -q '"hops"'; then
                local worst_hop=$(echo "$mtr_data" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    hops = d.get('hops', [])
    worst = max(hops, key=lambda h: h.get('loss', 0)) if hops else None
    if worst and worst.get('loss', 0) > 0:
        print(f\"hop{worst['hop']}:{worst['loss']:.0f}%\")
except: pass
" 2>/dev/null)
                [ -n "$worst_hop" ] && hop_info=" | $worst_hop"
            fi
            echo -e "${GREEN}$time_str - OK${NC} | ${latency}ms$hop_info"
        fi
        CONSECUTIVE_FAILURES=0
        
        # Log success
        echo "{\"ts\":\"$ts\",\"worker\":\"$CWORKER\",\"status\":\"ok\",\"latency_ms\":$latency}" >> "$METRICS_FILE"
        return 0
    else
        # Failure
        CONSECUTIVE_FAILURES=$((CONSECUTIVE_FAILURES + 1))
        
        local error_msg=""
        case $curl_exit in
            6)  error_msg="dns_failed" ;;
            7)  error_msg="connection_refused" ;;
            28) error_msg="timeout" ;;
            *)  error_msg="curl_error_$curl_exit" ;;
        esac
        
        # Get problem hop from MTR if available
        local problem_hop=""
        if echo "$mtr_data" | grep -q '"hops"'; then
            problem_hop=$(echo "$mtr_data" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    hops = d.get('hops', [])
    # Find first hop with >50% loss or timeout
    for h in hops:
        if h.get('loss', 0) > 50 or h.get('host') == 'timeout':
            print(f\"Problem at hop {h['hop']}: {h['host']} ({h.get('loss',0):.0f}% loss)\")
            break
except: pass
" 2>/dev/null)
        fi
        
        echo -e "${RED}$time_str - FAIL${NC} | $error_msg | HTTP:$http_code | Consecutive:$CONSECUTIVE_FAILURES"
        [ -n "$problem_hop" ] && echo -e "${YELLOW}  → $problem_hop${NC}"
        
        # Log failure with MTR data
        echo "{\"ts\":\"$ts\",\"worker\":\"$CWORKER\",\"status\":\"fail\",\"error\":\"$error_msg\",\"consecutive\":$CONSECUTIVE_FAILURES,\"mtr\":$mtr_data}" >> "$METRICS_FILE"
        
        return 1
    fi
}

# Startup
echo "=========================================="
echo "Uptime Client with MTR Logging"
echo "=========================================="
echo "Worker: $WORKER"
echo "Server: ${SERVER_ADDR}:${SERVER_PORT}"
echo "Ping Interval: ${PING_INTERVAL:-30}s"
echo "Logs: $LOG_DIR"
echo "=========================================="

# Initial MTR test
echo "Running initial MTR to $SERVER_ADDR..."
initial_mtr=$(run_mtr)
echo "$initial_mtr" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    hops = d.get('hops', [])
    print(f'Traced {len(hops)} hops, final loss: {d.get(\"loss\", \"?\"):.1f}%, avg: {d.get(\"avg\", \"?\"):.1f}ms')
    for h in hops:
        status = '✓' if h.get('loss', 0) == 0 else '✗' if h.get('loss', 0) > 50 else '~'
        print(f'  {status} Hop {h[\"hop\"]}: {h[\"host\"]} - {h.get(\"avg\", -1):.1f}ms ({h.get(\"loss\", 0):.0f}% loss)')
except Exception as e:
    print(f'MTR parse error: {e}')
" 2>/dev/null
echo "=========================================="
echo "Starting monitoring..."

# Main loop
while true; do
    rotate_logs
    do_ping
    sleep ${PING_INTERVAL:-30}
done
