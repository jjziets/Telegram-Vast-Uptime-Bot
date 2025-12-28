#!/bin/bash
# Enhanced Uptime Client with RCA (Root Cause Analysis) Logging
# Stores diagnostic data for retrospective analysis

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source $DIR/.env

WORKER=$1
LOG_DIR="${DIR}/logs"
METRICS_FILE="${LOG_DIR}/metrics.jsonl"
DIAG_FILE="${LOG_DIR}/diagnostics.jsonl"
MAX_LOG_SIZE=$((50*1024*1024))  # 50MB max

# Create log directory
mkdir -p "$LOG_DIR"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

# Counters
CONSECUTIVE_FAILURES=0

if ! [ -n "$WORKER" ]; then
  WORKER="unknown-worker"
fi

# Rotate log if too large
rotate_logs() {
    for f in "$METRICS_FILE" "$DIAG_FILE"; do
        if [ -f "$f" ] && [ $(stat -c%s "$f" 2>/dev/null || stat -f%z "$f" 2>/dev/null || echo 0) -gt $MAX_LOG_SIZE ]; then
            mv "$f" "${f}.$(date +%Y%m%d_%H%M%S).old"
        fi
    done
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

# Run network diagnostics and log
run_diagnostics() {
    local ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    local diag="{\"ts\":\"$ts\",\"worker\":\"$CWORKER\",\"type\":\"network_diag\""
    
    echo -e "${YELLOW}Running network diagnostics...${NC}"
    
    # Gateway check
    local gateway=$(ip route 2>/dev/null | grep default | awk '{print $3}' | head -1)
    if [ -n "$gateway" ]; then
        local gw_result=$(ping -c 3 -W 2 $gateway 2>&1)
        local gw_loss=$(echo "$gw_result" | grep -oP '\d+(?=% packet loss)' || echo "100")
        local gw_avg=$(echo "$gw_result" | grep -oP 'avg[^=]*=\s*[0-9.]+/\K[0-9.]+' || echo "-1")
        diag+=",\"gateway\":\"$gateway\",\"gw_loss_pct\":$gw_loss,\"gw_avg_ms\":$gw_avg"
    else
        diag+=",\"gateway\":\"none\",\"gw_loss_pct\":100,\"gw_avg_ms\":-1"
    fi
    
    # External connectivity (8.8.8.8)
    local ext_result=$(ping -c 3 -W 2 8.8.8.8 2>&1)
    local ext_loss=$(echo "$ext_result" | grep -oP '\d+(?=% packet loss)' || echo "100")
    local ext_avg=$(echo "$ext_result" | grep -oP 'avg[^=]*=\s*[0-9.]+/\K[0-9.]+' || echo "-1")
    diag+=",\"external_loss_pct\":$ext_loss,\"external_avg_ms\":$ext_avg"
    
    # Bot server ping
    local bot_result=$(ping -c 3 -W 3 $SERVER_ADDR 2>&1)
    local bot_loss=$(echo "$bot_result" | grep -oP '\d+(?=% packet loss)' || echo "100")
    local bot_avg=$(echo "$bot_result" | grep -oP 'avg[^=]*=\s*[0-9.]+/\K[0-9.]+' || echo "-1")
    diag+=",\"bot_loss_pct\":$bot_loss,\"bot_avg_ms\":$bot_avg"
    
    # Traceroute (quick, max 15 hops)
    local tr_output=$(traceroute -n -m 15 -w 2 $SERVER_ADDR 2>&1 | tail -n +2 | head -15)
    # Extract hop info as compact string
    local tr_hops=$(echo "$tr_output" | awk '{printf "%s:%s,", $1, $2}' | sed 's/,$//')
    diag+=",\"traceroute\":\"$tr_hops\""
    
    # DNS check
    local dns_start=$(date +%s%3N)
    local dns_ok=$(host $SERVER_ADDR >/dev/null 2>&1 && echo "1" || echo "0")
    local dns_end=$(date +%s%3N)
    local dns_ms=$((dns_end - dns_start))
    diag+=",\"dns_ok\":$dns_ok,\"dns_ms\":$dns_ms"
    
    # TCP port check
    local tcp_ok=$(timeout 3 bash -c "echo '' > /dev/tcp/$SERVER_ADDR/$SERVER_PORT" 2>&1 && echo "1" || echo "0")
    diag+=",\"tcp_port_ok\":$tcp_ok"
    
    diag+="}"
    echo "$diag" >> "$DIAG_FILE"
    
    # Analyze and print likely cause
    if [ "$gw_loss" = "100" ]; then
        echo -e "${RED}>>> CAUSE: Local network/gateway DOWN <<<${NC}"
    elif [ "$ext_loss" = "100" ]; then
        echo -e "${RED}>>> CAUSE: ISP/Upstream connectivity FAILED <<<${NC}"
    elif [ "$bot_loss" = "100" ] && [ "$tcp_ok" = "0" ]; then
        echo -e "${RED}>>> CAUSE: Bot server unreachable (network path or server down) <<<${NC}"
    elif [ "$bot_loss" = "100" ] && [ "$tcp_ok" = "1" ]; then
        echo -e "${YELLOW}>>> CAUSE: Bot server dropping ICMP but TCP OK (possible DDoS mitigation) <<<${NC}"
    elif [ "$bot_loss" -gt 50 ]; then
        echo -e "${YELLOW}>>> CAUSE: High packet loss to bot server (${bot_loss}%) <<<${NC}"
    else
        echo -e "${YELLOW}>>> CAUSE: Unknown - check diagnostics log <<<${NC}"
    fi
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
        # Success
        CONSECUTIVE_FAILURES=0
        echo -e "${GREEN}$time_str - OK${NC} | $CWORKER | ${latency}ms"
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
        
        echo -e "${RED}$time_str - FAIL${NC} | $CWORKER | Error: $error_msg | HTTP: $response | Consecutive: $CONSECUTIVE_FAILURES"
        log_metric "fail" "$response" "$latency" "$error_msg"
        
        # Run diagnostics on 2nd consecutive failure
        if [ $CONSECUTIVE_FAILURES -eq 2 ]; then
            run_diagnostics
        fi
        
        # Warning on extended outage
        if [ $CONSECUTIVE_FAILURES -eq 6 ]; then
            echo -e "${RED}!!! EXTENDED OUTAGE: Will be marked DOWN soon !!!${NC}"
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
echo "Metrics Log: $METRICS_FILE"
echo "Diagnostics Log: $DIAG_FILE"
echo "=========================================="

# Initial diagnostics
echo "Running initial network check..."
run_diagnostics

# Main loop
while true; do
    rotate_logs
    do_ping
    sleep ${PING_INTERVAL:-30}
done

