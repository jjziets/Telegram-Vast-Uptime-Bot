#!/bin/bash

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
set -o allexport
source $DIR/.env

PARAM=$1

if [ "$PARAM" == "chat_id" ]; then
  python3 $DIR/lib/get_chat_id.py
  exit
fi

python3 $DIR/lib/server.py
