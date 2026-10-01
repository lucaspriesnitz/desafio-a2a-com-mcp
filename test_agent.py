import urllib.request
import json

# Testar o Agent Card
print("Testando Agent Card...")
req = urllib.request.Request('http://localhost:7300/.well-known/agent-card.json')
try:
    resp = urllib.request.urlopen(req, timeout=5)
    card = json.loads(resp.read().decode())
    print(f"OK: {resp.status}")
    print(f"Name: {card.get('name')}")
    print(f"Skills: {[s.get('id') for s in card.get('skills', [])]}")
except Exception as e:
    print(f"ERROR: {e}")

# Testar SendMessage simples
print("\nTestando SendMessage...")
body = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "SendMessage",
    "params": {
        "message": {
            "messageId": "msg-test",
            "role": "ROLE_USER",
            "parts": [{"text": "reservar sala=sala-porao inicio=2026-11-03T09:00:00-03:00 fim=2026-11-03T10:00:00-03:00 responsavel=Doc"}]
        }
    }
}

req = urllib.request.Request(
    'http://localhost:7300/a2a',
    data=json.dumps(body).encode(),
    headers={'Content-Type': 'application/json'},
    method='POST'
)

try:
    resp = urllib.request.urlopen(req, timeout=10)
    result = json.loads(resp.read().decode())
    print(f"OK: {resp.status}")
    task = result.get('result', {}).get('task', {})
    print(f"Task ID: {task.get('id')}")
    print(f"State: {task.get('status', {}).get('state')}")
except Exception as e:
    print(f"ERROR: {e}")
