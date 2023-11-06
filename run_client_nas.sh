#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source $DIR/.env
WORKER=$1
if ! [ -n "$WORKER" ]; then
  WORKER="unknown-worker"
fi
while [ 1 ]
do
  # Check if mfschunkserver is running
  if pgrep mfschunkserver > /dev/null
  then
    echo "mfschunkserver is running."
    # Existing code to ping the server
    CWORKER="$WORKER"
    request_url="http://$SERVER_ADDR:$SERVER_PORT/ping/$CWORKER?api_key=$API_KEY"
    time=`date "+%H:%M:%S-%d/%m/%Y"`
    echo "$time - Pinging $request_url"
    curl -m 2 "$request_url"
  else
    echo "mfschunkserver is not running."
    # Handle the case where mfschunkserver is not running
    # You can add code here to start it or log this incident
  fi
  sleep $PING_INTERVAL
done
