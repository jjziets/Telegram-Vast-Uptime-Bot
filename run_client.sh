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
  if [ $? -eq 0 ]; then
    # If nvidia-smi succeeds
    numGPUs=$(nvidia-smi --query-gpu=count --format=csv,noheader -i 0)
    CWORKER="$WORKER($numGPUs)"
  else
    # If nvidia-smi fails
    echo -e "\033[0;31m$(date "+%H:%M:%S-%d/%m/%Y") - ERROR: nvidia-smi command failed.\033[0m"
    sleep $PING_INTERVAL
    continue
  fi

  request_url="http://$SERVER_ADDR:$SERVER_PORT/ping/$CWORKER?api_key=$API_KEY"
  time=`date "+%H:%M:%S-%d/%m/%Y"`
  echo "$time - Pinging $request_url"

  # Using curl with a timeout and capturing the response and HTTP status code
  response=$(curl -m $FAIL_TIMEOUT -s -o /dev/null -w "%{http_code}" "$request_url")
  if [ "$response" -ne 200 ]; then
    # If the server cannot be reached or returns a non-200 status
    echo -e "\033[0;31m$time - ERROR: Failed to reach server at $request_url or server returned error. HTTP status: $response.\033[0m"
  fi

  sleep $PING_INTERVAL
done
