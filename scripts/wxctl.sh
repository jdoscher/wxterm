#!/usr/bin/env bash
# Control the running display from any shell on the host (the display polls its state file every 2 s).
#   scripts/wxctl.sh location sf      scripts/wxctl.sh theme green      scripts/wxctl.sh font dots|segment|blocks
#   scripts/wxctl.sh aprs off|wx|all|map                (the APRS view, same as pressing x)
#   scripts/wxctl.sh gif              (records one radar loop to ~/gifs, same as pressing g)
set -euo pipefail
. "$(dirname "$0")/../kiosk/lib.sh"
state="${WXTERM_STATE:-$STATE_DIR/state.json}"
[[ -e "$state" || -w "$(dirname "$state")" ]] || state="$HOME/.config/wxterm/state.json"
cmd="${1:?usage: wxctl.sh location NAME | theme NAME | font NAME | aprs VIEW | gif | mcp}"
val="${2:-}"
if [[ "$cmd" == mcp ]]; then   # print how to connect an LLM client to the MCP server
  envf="$WXTERM_DIR/.env"
  [[ -r "$envf" ]] || { echo "can't read $envf (run as the deploy user or with sudo)"; exit 1; }
  tok=$(sed -n 's/^MCP_TOKEN=//p' "$envf")
  [[ -n "$tok" ]] || { echo "MCP is off: set MCP=yes in kiosk/kiosk.conf and rerun sudo kiosk/setup.sh"; exit 1; }
  port=$(sed -n 's/^MCP_PORT=//p' "$envf"); bind=$(sed -n 's/^MCP_BIND=//p' "$envf")
  host="$bind"; [[ "$bind" == 0.0.0.0 ]] && host=$(hostname -I | awk '{print $1}')
  url="http://$host:${port:-8090}/mcp"
  cat <<INFO
URL:    $url
Token:  $tok
Health: $(curl -s -m 3 "http://127.0.0.1:${port:-8090}/healthz" || echo "not answering")

Claude Code:
  claude mcp add --transport http wxterm $url --header "Authorization: Bearer $tok"
Claude Desktop and other stdio-only clients (mcpServers in their config file):
  "wxterm": {"command": "npx", "args": ["-y", "mcp-remote", "$url", "--allow-http",
             "--header", "Authorization: Bearer $tok"]}
Any client that speaks streamable HTTP: the URL above plus the header "Authorization: Bearer <token>".
INFO
  exit 0
fi
python3 - "$state" "$cmd" "$val" <<'PY'
import json, os, sys, time
path, cmd, val = sys.argv[1:]
try:
    st = json.load(open(path))
except (OSError, ValueError):
    st = {}
if cmd == "location":
    st["location"] = val.lower()
elif cmd == "theme":
    st["theme"] = val.upper()
elif cmd == "font":
    st["bigfont"] = val.lower()
elif cmd == "aprs":
    if val.upper() not in ("OFF", "WX", "ALL", "MAP"):
        sys.exit("aprs view: off, wx, all or map")
    st["aprs_view"] = val.upper()
elif cmd == "gif":
    st["gif"] = str(time.time())
else:
    sys.exit(f"unknown command {cmd}")
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp"
json.dump(st, open(tmp, "w"))
os.chmod(tmp, 0o664)
os.replace(tmp, path)
print(json.dumps(st))
PY
