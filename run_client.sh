#!/bin/bash

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

source $DIR/.env

WORKER=$1


if ! [ -n "$WORKER" ]; then
  WORKER="unknown-worker"
fi


while [ 1 ]
do
  driver_check=$(nvidia-smi | grep "Driver Version:")
  if [ -n  "$driver_check" ]; then
        # Get Number of Connected GPUs                                           {{{1
        numGPUs=$(nvidia-smi --query-gpu=count --format=csv,noheader -i 0)
        CWORKER="$WORKER($numGPUs)"
        request_url="http://$SERVER_ADDR:$SERVER_PORT/ping/$CWORKER?api_key=$API_KEY"
        time=`date "+%H:%M:%S-%d/%m/%Y"`
        echo "$time - Pinging $request_url"
        curl -m 2 "$request_url"
  fi
  sleep $PING_INTERVAL
done
