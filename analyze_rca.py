#!/usr/bin/env python3
"""
RCA Log Analyzer
Analyzes metrics and diagnostics logs to determine root cause of outages

Usage:
    python3 analyze_rca.py metrics.jsonl [diagnostics.jsonl]
    python3 analyze_rca.py --server http://159.65.255.55:5000
"""

import json
import sys
import argparse
from datetime import datetime, timedelta
from collections import defaultdict

def parse_jsonl(filename):
    """Parse JSONL file"""
    events = []
    with open(filename, 'r') as f:
        for line in f:
            try:
                events.append(json.loads(line.strip()))
            except:
                pass
    return events

def analyze_metrics(metrics):
    """Analyze client metrics log"""
    if not metrics:
        print("No metrics data")
        return
    
    # Group by status
    ok_count = len([m for m in metrics if m.get('status') == 'ok'])
    fail_count = len([m for m in metrics if m.get('status') == 'fail'])
    total = len(metrics)
    
    print(f"\n{'='*60}")
    print("METRICS SUMMARY")
    print(f"{'='*60}")
    print(f"Total pings: {total}")
    print(f"Successful: {ok_count} ({100*ok_count/total:.1f}%)")
    print(f"Failed: {fail_count} ({100*fail_count/total:.1f}%)")
    
    if fail_count == 0:
        print("\n✅ No failures detected")
        return
    
    # Analyze failure patterns
    failures = [m for m in metrics if m.get('status') == 'fail']
    
    # Group by error type
    error_types = defaultdict(int)
    for f in failures:
        error_types[f.get('error', 'unknown')] += 1
    
    print(f"\nFailure breakdown:")
    for err, count in sorted(error_types.items(), key=lambda x: -x[1]):
        print(f"  {err}: {count} ({100*count/fail_count:.1f}%)")
    
    # Find failure clusters (consecutive failures)
    print(f"\nFailure clusters (consecutive failures):")
    clusters = []
    current_cluster = []
    
    for m in metrics:
        if m.get('status') == 'fail':
            current_cluster.append(m)
        else:
            if len(current_cluster) >= 2:
                clusters.append(current_cluster)
            current_cluster = []
    
    if len(current_cluster) >= 2:
        clusters.append(current_cluster)
    
    for i, cluster in enumerate(clusters[-10:], 1):  # Last 10 clusters
        start = cluster[0].get('ts', 'unknown')
        end = cluster[-1].get('ts', 'unknown')
        errors = set(c.get('error') for c in cluster)
        print(f"  Cluster {i}: {start} to {end} ({len(cluster)} failures)")
        print(f"    Errors: {', '.join(errors)}")

def analyze_diagnostics(diags):
    """Analyze client diagnostics log"""
    if not diags:
        print("\nNo diagnostics data")
        return
    
    print(f"\n{'='*60}")
    print("DIAGNOSTICS ANALYSIS")
    print(f"{'='*60}")
    print(f"Total diagnostic runs: {len(diags)}")
    
    # Analyze each diagnostic
    issues = defaultdict(int)
    
    for d in diags:
        if d.get('gw_loss_pct', 0) == 100:
            issues['gateway_down'] += 1
        elif d.get('gw_loss_pct', 0) > 50:
            issues['gateway_packet_loss'] += 1
        
        if d.get('external_loss_pct', 0) == 100:
            issues['external_down'] += 1
        elif d.get('external_loss_pct', 0) > 50:
            issues['external_packet_loss'] += 1
        
        if d.get('bot_loss_pct', 0) == 100:
            if d.get('tcp_port_ok', 0) == 0:
                issues['bot_unreachable'] += 1
            else:
                issues['bot_icmp_blocked'] += 1
        elif d.get('bot_loss_pct', 0) > 50:
            issues['bot_packet_loss'] += 1
        
        if d.get('dns_ok', 1) == 0:
            issues['dns_failure'] += 1
    
    print(f"\nIssue breakdown:")
    for issue, count in sorted(issues.items(), key=lambda x: -x[1]):
        print(f"  {issue}: {count} ({100*count/len(diags):.1f}%)")
    
    # Determine most likely root cause
    print(f"\n{'='*60}")
    print("ROOT CAUSE ANALYSIS")
    print(f"{'='*60}")
    
    if issues.get('gateway_down', 0) > len(diags) * 0.3:
        print("🔴 PRIMARY CAUSE: Local gateway/network down")
        print("   → Check local router, switch, or network interface")
    elif issues.get('external_down', 0) > len(diags) * 0.3:
        print("🔴 PRIMARY CAUSE: ISP/Upstream connectivity failure")
        print("   → Contact ISP, check upstream router")
    elif issues.get('bot_unreachable', 0) > len(diags) * 0.3:
        print("🔴 PRIMARY CAUSE: Bot server unreachable")
        print("   → Check bot server, route to DigitalOcean, DDoS")
    elif issues.get('dns_failure', 0) > len(diags) * 0.3:
        print("🟡 PRIMARY CAUSE: DNS resolution failures")
        print("   → Check DNS servers, network config")
    elif issues.get('bot_packet_loss', 0) > len(diags) * 0.3:
        print("🟡 PRIMARY CAUSE: High packet loss to bot server")
        print("   → Network congestion, routing issues")
    else:
        print("🟢 No clear pattern - likely intermittent issues")
        print("   → Review traceroute data for specific failures")
    
    # Show recent traceroutes
    print(f"\nRecent traceroute samples:")
    for d in diags[-3:]:
        ts = d.get('ts', 'unknown')
        tr = d.get('traceroute', 'N/A')
        print(f"  [{ts}] {tr[:100]}...")

def analyze_server_events(events):
    """Analyze server event log"""
    if not events:
        print("No server events")
        return
    
    print(f"\n{'='*60}")
    print("SERVER EVENT ANALYSIS")
    print(f"{'='*60}")
    
    up_events = [e for e in events if e.get('type') == 'up']
    down_events = [e for e in events if e.get('type') == 'down']
    
    print(f"Total events: {len(events)}")
    print(f"UP events: {len(up_events)}")
    print(f"DOWN events: {len(down_events)}")
    
    # Find mass failures (multiple workers down within 1 minute)
    print(f"\nMass failure detection:")
    down_by_time = defaultdict(list)
    for e in down_events:
        ts = e.get('ts', '')[:16]  # Group by minute
        down_by_time[ts].append(e.get('worker'))
    
    mass_events = [(ts, workers) for ts, workers in down_by_time.items() if len(workers) >= 3]
    
    if mass_events:
        print(f"  Found {len(mass_events)} mass failure events:")
        for ts, workers in mass_events[:10]:
            print(f"    [{ts}] {len(workers)} workers: {', '.join(workers[:5])}...")
        print("\n  🔴 Mass failures indicate NETWORK ISSUE (not individual servers)")
    else:
        print("  No mass failures detected")
        print("  Failures appear to be individual server issues")
    
    # Worker-specific analysis
    print(f"\nWorker failure frequency:")
    worker_downs = defaultdict(int)
    for e in down_events:
        worker_downs[e.get('worker')] += 1
    
    for worker, count in sorted(worker_downs.items(), key=lambda x: -x[1])[:10]:
        print(f"  {worker}: {count} down events")

def main():
    parser = argparse.ArgumentParser(description='Analyze RCA logs')
    parser.add_argument('metrics_file', nargs='?', help='Metrics JSONL file')
    parser.add_argument('diagnostics_file', nargs='?', help='Diagnostics JSONL file')
    parser.add_argument('--server', help='Server URL for API analysis')
    parser.add_argument('--events', help='Server events JSONL file')
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("UPTIME MONITOR RCA ANALYZER")
    print("=" * 60)
    
    if args.server:
        import urllib.request
        try:
            with urllib.request.urlopen(f"{args.server}/api/rca") as r:
                data = json.loads(r.read())
                print(json.dumps(data, indent=2))
        except Exception as e:
            print(f"Error fetching from server: {e}")
        return
    
    if args.metrics_file:
        metrics = parse_jsonl(args.metrics_file)
        analyze_metrics(metrics)
    
    if args.diagnostics_file:
        diags = parse_jsonl(args.diagnostics_file)
        analyze_diagnostics(diags)
    
    if args.events:
        events = parse_jsonl(args.events)
        analyze_server_events(events)
    
    if not args.metrics_file and not args.events and not args.server:
        parser.print_help()

if __name__ == '__main__':
    main()

