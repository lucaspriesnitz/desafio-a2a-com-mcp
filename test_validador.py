import sys
import urllib.request
import json

sys.path.insert(0, 'C:/Users/Lucas/Documents/dev/mba/desafio-a2a-com-mcp/validador')

# Importar apenas as funções necessárias
from validar import mcp, R

# Testar apenas a primeira verificação
print("Testando tools/list...")
status, resposta = mcp('http://localhost:7301/mcp', 'tools/list', {})
tools = {t["name"]: t for t in (resposta.get("result") or {}).get("tools", [])}
esperadas = {"listar_salas", "consultar_disponibilidade", "reservar_sala"}
print(f"Tools encontradas: {sorted(tools.keys())}")
print(f"Esperadas: {sorted(esperadas)}")
print(f"Match: {esperadas <= set(tools)}")
