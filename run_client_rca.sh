#!/bin/bash
# Enhanced Uptime Client with RCA (Root Cause Analysis) Logging
# - Collects MTR data on each ping
# - Sends diagnostic backlog when recovering from failure
# - Fixed traceroute/mtr parsing

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source $DIR/.env

WORKER=$1
LOG_DIR="${DIR}/logs"
METRICS_FILE="${LOG_DIR}/metrics.jsonl"
DIAG_FILE="${LOG_DIR}/diagnostics.jsonl"
MTR_DIR="${LOG_DIR}/mtr"
BACKLOG_FILE="${LOG_DIR}/backlog.jsonl"
MAX_LOG_SIZE=$((50*1024*1024))  # 50MB max
MAX_BACKLOG=10  # Keep last N diagnostic reports for backlog

# Create directories
mkdir -p "$LOG_DIR" "$MTR_DIR"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# State
CONSECUTIVE_FAILURES=0
LAST_STATUS="unknown"
CWORKER=""

if ! [ -n "$WORKER" ]; then
  WORKER="unknown-worker"
fi

# Rotate log if too large
rotate_logs() {
    for f in "$METRICS_FILE" "$DIAG_FILE" "$BACKLOG_FILE"; do
        if [ -f "$f" ] && [ $(stat -c%s "$f" 2>/dev/null || stat -f%z "$f" 2>/dev/null || echo 0) -gt $MAX_LOG_SIZE ]; then
            mv "$f" "${f}.$(date +%Y%m%d_%H%M%S).old"
        fi
    done
    # Clean old MTR files (keep last 100)
    ls -t "$MTR_DIR"/*.json 2>/dev/null | tail -n +100 | xargs rm -f 2>/dev/null
}

# Log metric as JSON line
log_metric() {
    local status=$1
    local http_code=$2
    local latency=$3
    local error=$4
    
    local ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    echo "{\"ts\":\"$ts\",\"worker\":\"$CWORKER\",\"status\":\"$status\",\"http\":\"$http_code\",\"latency_ms\":$latency,\"error\":\"$error\",\"consecutive_failures\":$CONSECUTIVE_FAILURES}" >> "$METRICS_FILE"
}

# Run MTR and return JSON
run_mtr() {
    local target=$1
    local ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    local mtr_file="${MTR_DIR}/mtr_$(date +%Y%m%d_%H%M%S).json"
    
    # Run MTR with JSON output if available, otherwise parse text
    if mtr --version 2>&1 | grep -q "mtr"; then
        # Try JSON output first (newer mtr)
        local mtr_json=$(mtr -j -c 3 -w "$target" 2>/dev/null)
        if [ -n "$mtr_json" ] && echo "$mtr_json" | grep -q "report"; then
            echo "$mtr_json" > "$mtr_file"
            echo "$mtr_json"
            return
        fi
    fi
    
    # Fallback: parse text MTR output
    local mtr_text=$(mtr -r -c 3 -w "$target" 2>/dev/null || mtr -r -c 3 "$target" 2>/dev/null)
    if [ -n "$mtr_text" ]; then
        # Parse MTR text into JSON
        local hops=$(echo "$mtr_text" | tail -n +2 | awk '{
            gsub(/%/, "", $3);
            printf "{\"hop\":%d,\"host\":\"%s\",\"loss\":%.1f,\"avg\":%.1f},", $1, $2, $3, $6
        }' | sed 's/,$//')
        
        local result="{\"ts\":\"$ts\",\"target\":\"$target\",\"hops\":[$hops]}"
        echo "$result" > "$mtr_file"
        echo "$result"
        return
    fi
    
    # Last resort: use traceroute
    local tr_text=$(traceroute -n -m 15 -w 2 "$target" 2>/dev/null)
    if [ -n "$tr_text" ]; then
        local hops=$(echo "$tr_text" | tail -n +2 | awk '{
            host = ($2 == "*") ? "timeout" : $2;
            # Extract time from column that contains "ms"
            time = -1;
            for(i=3; i<=NF; i++) {
                if($i ~ /^[0-9.]+$/ && $(i+1) == "ms") { time = $i; break; }
            }
            printf "{\"hop\":%d,\"host\":\"%s\",\"time\":%.1f},", $1, host, time
        }' | sed 's/,$//')
        
        local result="{\"ts\":\"$ts\",\"target\":\"$target\",\"type\":\"traceroute\",\"hops\":[$hops]}"
        echo "$result" > "$mtr_file"
        echo "$result"
        return
    fi
    
    echo "{\"ts\":\"$ts\",\"target\":\"$target\",\"error\":\"mtr/traceroute unavailable\"}"
}

# Run full network diagnostics
run_diagnostics() {
    local ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    echo -e "${CYAN}Running network diagnostics...${NC}"
    
    # Gateway check
    local gateway=$(ip route 2>/dev/null | grep default | awk '{print $3}' | head -1)
    local gw_loss=100
    local gw_avg=-1
    if [ -n "$gateway" ]; then
        local gw_result=$(ping -c 3 -W 2 "$gateway" 2>&1)
        gw_loss=$(echo "$gw_result" | grep -oP '\d+(?=% packet loss)' || echo "100")
        gw_avg=$(echo "$gw_result" | grep -oP 'rtt [^=]*= [0-9.]+/\K[0-9.]+' || echo "-1")
    fi
    
    # External connectivity (8.8.8.8)
    local ext_result=$(ping -c 3 -W 2 8.8.8.8 2>&1)
    local ext_loss=$(echo "$ext_result" | grep -oP '\d+(?=% packet loss)' || echo "100")
    local ext_avg=$(echo "$ext_result" | grep -oP 'rtt [^=]*= [0-9.]+/\K[0-9.]+' || echo "-1")
    
    # Bot server ping
    local bot_result=$(ping -c 3 -W 3 "$SERVER_ADDR" 2>&1)
    local bot_loss=$(echo "$bot_result" | grep -oP '\d+(?=% packet loss)' || echo "100")
    local bot_avg=$(echo "$bot_result" | grep -oP 'rtt [^=]*= [0-9.]+/\K[0-9.]+' || echo "-1")
    
    # DNS check
    local dns_start=$(date +%s%3N)
    local dns_ok=0
    if host "$SERVER_ADDR" >/dev/null 2>&1 || getent hosts "$SERVER_ADDR" >/dev/null 2>&1; then
        dns_ok=1
    fi
    local dns_end=$(date +%s%3N)
    local dns_ms=$((dns_end - dns_start))
    
    # TCP port check
    local tcp_ok=0
    if timeout 3 bash -c "echo '' > /dev/tcp/$SERVER_ADDR/$SERVER_PORT" 2>/dev/null; then
        tcp_ok=1
    fi
    
    # Run MTR to bot server
    local mtr_data=$(run_mtr "$SERVER_ADDR")
    
    # Build diagnostics JSON
    local diag=$(cat <<EOF
{"ts":"$ts","worker":"$CWORKER","gateway":"${gateway:-none}","gw_loss_pct":$gw_loss,"gw_avg_ms":${gw_avg:--1},"external_loss_pct":$ext_loss,"external_avg_ms":${ext_avg:--1},"bot_loss_pct":$bot_loss,"bot_avg_ms":${bot_avg:--1},"dns_ok":$dns_ok,"dns_ms":$dns_ms,"tcp_port_ok":$tcp_ok,"mtr":$mtr_data}
EOF
)
    
    # Log diagnostics
    echo "$diag" >> "$DIAG_FILE"
    
    # Add to backlog (for sending on recovery)
    echo "$diag" >> "$BACKLOG_FILE"
    # Trim backlog to last N entries
    tail -n $MAX_BACKLOG "$BACKLOG_FILE" > "${BACKLOG_FILE}.tmp" && mv "${BACKLOG_FILE}.tmp" "$BACKLOG_FILE"
    
    # Analyze and print likely cause
    echo -e "${CYAN}Diagnostics: gw=$gw_loss% ext=$ext_loss% bot=$bot_loss% tcp=$tcp_ok${NC}"
    
    if [ "$gw_loss" = "100" ]; then
        echo -e "${RED}>>> CAUSE: Local gateway/network DOWN <<<${NC}"
    elif [ "$ext_loss" = "100" ]; then
        echo -e "${RED}>>> CAUSE: ISP/Upstream connectivity FAILED <<<${NC}"
    elif [ "$bot_loss" = "100" ] && [ "$tcp_ok" = "0" ]; then
        echo -e "${RED}>>> CAUSE: Bot server unreachable <<<${NC}"
    elif [ "$bot_loss" = "100" ] && [ "$tcp_ok" = "1" ]; then
        echo -e "${YELLOW}>>> CAUSE: ICMP blocked but TCP OK (DDoS mitigation?) <<<${NC}"
    elif [ "$bot_loss" -gt 50 ] 2>/dev/null; then
        echo -e "${YELLOW}>>> CAUSE: High packet loss (${bot_loss}%) <<<${NC}"
    fi
    
    echo "$diag"
}

# Send backlog to server on recovery
send_backlog() {
    if [ ! -f "$BACKLOG_FILE" ]; then
        return
    fi
    
    echo -e "${CYAN}Sending diagnostic backlog to server...${NC}"
    
    # Read backlog and send as JSON array
    local backlog_json=$(cat "$BACKLOG_FILE" | jq -s '.' 2>/dev/null)
    if [ -z "$backlog_json" ] || [ "$backlog_json" = "null" ]; then
        # jq not available, send as-is
        backlog_json="[$(cat "$BACKLOG_FILE" | tr '\n' ',' | sed 's/,$//' )]"
    fi
    
    # POST to server
    local request_url="http://${SERVER_ADDR}:${SERVER_PORT}/ping/${CWORKER}?api_key=${API_KEY}"
    curl -s -X POST -H "Content-Type: application/json" \
        -d "{\"backlog\":$backlog_json,\"recovery\":true}" \
        "$request_url" >/dev/null 2>&1
    
    # Clear backlog after sending
    > "$BACKLOG_FILE"
}

# Main ping function
do_ping() {
    # Check nvidia-smi
    local driver_check=$(nvidia-smi 2>&1 | grep "Driver Version:")
    if [ $? -eq 0 ]; then
        local numGPUs=$(nvidia-smi --query-gpu=count --format=csv,noheader -i 0 2>/dev/null)
        CWORKER="${WORKER}(${numGPUs})"
    else
        echo -e "${RED}$(date '+%H:%M:%S') - nvidia-smi failed${NC}"
        CWORKER="${WORKER}(GPU_ERR)"
        log_metric "gpu_error" "0" "0" "nvidia-smi failed"
        CONSECUTIVE_FAILURES=$((CONSECUTIVE_FAILURES + 1))
        return 1
    fi
    
    local request_url="http://${SERVER_ADDR}:${SERVER_PORT}/ping/${CWORKER}?api_key=${API_KEY}"
    local time_str=$(date "+%H:%M:%S-%d/%m/%Y")
    
    # Capture timing
    local start_ms=$(date +%s%3N)
    local response=$(curl -m ${FAIL_TIMEOUT:-30} -s -o /dev/null -w "%{http_code}" "$request_url" 2>&1)
    local curl_exit=$?
    local end_ms=$(date +%s%3N)
    local latency=$((end_ms - start_ms))
    
    if [ $curl_exit -eq 0 ] && [ "$response" = "200" ]; then
        # Success - check if recovering from failure
        if [ $CONSECUTIVE_FAILURES -gt 0 ]; then
            echo -e "${GREEN}$time_str - RECOVERED${NC} after $CONSECUTIVE_FAILURES failures"
            # Send backlog with diagnostics
            send_backlog
        else
            echo -e "${GREEN}$time_str - OK${NC} | $CWORKER | ${latency}ms"
        fi
        
        CONSECUTIVE_FAILURES=0
        LAST_STATUS="ok"
        log_metric "ok" "$response" "$latency" ""
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
        
        echo -e "${RED}$time_str - FAIL${NC} | Error: $error_msg | HTTP: $response | Consecutive: $CONSECUTIVE_FAILURES"
        log_metric "fail" "$response" "$latency" "$error_msg"
        LAST_STATUS="fail"
        
        # Run diagnostics on 2nd failure and then every 5th
        if [ $CONSECUTIVE_FAILURES -eq 2 ] || [ $((CONSECUTIVE_FAILURES % 5)) -eq 0 ]; then
            run_diagnostics >/dev/null
        fi
        
        # Warning on extended outage
        if [ $CONSECUTIVE_FAILURES -eq 6 ]; then
            echo -e "${RED}!!! EXTENDED OUTAGE: Will be marked DOWN !!!${NC}"
        fi
        
        return 1
    fi
}

# Startup
echo "=========================================="
echo "Enhanced Uptime Client with RCA Logging"
echo "=========================================="
echo "Worker: $WORKER"
echo "Server: ${SERVER_ADDR}:${SERVER_PORT}"
echo "Ping Interval: ${PING_INTERVAL}s"
echo "Fail Timeout: ${FAIL_TIMEOUT}s"
echo "Logs: $LOG_DIR"
echo "=========================================="

# Initial diagnostics
echo "Running initial network check..."
run_diagnostics >/dev/null
echo "Initial check complete. Starting monitoring..."

# Main loop
while true; do
    rotate_logs
    do_ping
    sleep ${PING_INTERVAL:-30}
done
