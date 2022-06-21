#!/bin/bash

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

source $DIR/.env

WORKER=$1


if ! [ -n "$WORKER" ]; then
  WORKER="unknown-worker"
fi


while [ 1 ]
do
  PUBIP="curl: (6) Could not resolve host: ifconfig.me."
  while [ "$PUBIP" = "curl: (6) Could not resolve host: ifconfig.me." -o "$PUBIP" = "" ]
        do
                PUBIP="$(curl -m 2 ifconfig.me.)"
                sleep 1
        done
  CWORKER="$PUBIP-$WORKER"

  request_url="http://$SERVER_ADDR:$SERVER_PORT/ping/$CWORKER?api_key=$API_KEY"
  time=`date "+%H:%M:%S-%d/%m/%Y"`
  echo "$time - Pinging $request_url"
  curl -m 2 "$request_url"
  sleep $PING_INTERVAL
done
