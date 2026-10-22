#!/bin/sh
cd "$(dirname "$0")"
( sleep 1; (command -v open >/dev/null && open http://127.0.0.1:8780) || (command -v xdg-open >/dev/null && xdg-open http://127.0.0.1:8780) ) &
exec python3 server.py "$@"
