import urllib.request
import json

url = 'http://localhost:7301/mcp'
body = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list",
    "params": {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {
                "elicitation": {"form": {}}
            }
        }
    }
}

req = urllib.request.Request(
    url,
    data=json.dumps(body).encode(),
    headers={
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream',
        'MCP-Protocol-Version': '2026-07-28',
        'Mcp-Method': 'tools/list'
    },
    method='POST'
)

try:
    resp = urllib.request.urlopen(req, timeout=5)
    print('OK:', resp.status)
    print(resp.read().decode()[:200])
except Exception as e:
    print('ERROR:', e)
