# A Ponte: Agente A2A com MCP

Desafio do MBA Engenharia de Software com IA (curso de MCP e A2A). Dois processos
independentes: um servidor MCP em Streamable HTTP que expõe as salas da Hill Valley
Tech, e um agente que é **host MCP por dentro** e **servidor A2A por fora**.

```
┌─────────────┐   A2A    ┌─────────────┐   MCP    ┌─────────────┐
│  Cliente    │ ───────> │   Agente    │ ───────> │  Servidor   │
│  A2A        │ <─────── │   (7300)    │ <─────── │  MCP (7301) │
└─────────────┘          └─────────────┘          └─────────────┘
```

O agente não tem LLM e não implementa regra de sala: ele traduz protocolo. Conflito,
política e cálculo de alternativas são decisão do servidor MCP.

---

## 1. Como rodar

A partir de um clone limpo, com Python 3.10 ou superior:

```bash
pip install -e .
```

O servidor sela o `requestState` com uma chave que vem **só do ambiente**. Gere a sua
e exporte antes de subir os processos — o valor nunca entra no repositório:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

```bash
# Linux/macOS
export REQUEST_STATE_SECRET=<o valor gerado acima>

# Windows (cmd)
for /f %s in ('python -c "import secrets; print(secrets.token_hex(32))"') do set REQUEST_STATE_SECRET=%s
```

**Terminal 1 — servidor MCP (porta 7301, endpoint `/mcp`).** Deixe o stderr visível:

```bash
python servidor-mcp/server.py
```

**Terminal 2 — agente A2A (porta 7300, endpoints `/a2a` e `/.well-known/agent-card.json`):**

```bash
python agente/agent.py
```

**Terminal 3 — validador:**

```bash
python validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Os dois hosts acima são os defaults do validador, então `python validador/validar.py`
sozinho também funciona.

No Windows, `start.bat` sobe os dois processos em janelas separadas. Ele **exige** que
`REQUEST_STATE_SECRET` já esteja no ambiente e falha explicitamente se não estiver.

---

## 2. Onde a ponte acontece

A ponte são quatro pontos em `agente/agent.py`:

**`input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED` da Task** — em
`_start_task`, [`agent.py:262-276`](agente/agent.py). Ao ver `resultType == "input_required"`
na resposta do `tools/call` (`:262`), o agente não responde a elicitation por conta
própria e não trava esperando: ele muda o estado da Task (`:264`), guarda o
`requestState` **no objeto da Task** (`:266`, campo declarado em `:143`) e devolve ao
cliente A2A a linha `alternativas: <ids>` na ordem exata do `enum` da elicitation
(`:275`). O mesmo tratamento existe em `_continue_task` (`:424-433`), para o caso de a
escolha do cliente cair em outro conflito.

**O `requestState` volta para o servidor** — em `_continue_task`,
[`agent.py:361`](agente/agent.py). O agente repete o `tools/call` original levando
`inputResponses` com a **mesma chave** que veio no `inputRequests` e o `requestState`
ecoado sem qualquer modificação. O envelope é montado em `MCPClient._call`
([`agent.py:80-81`](agente/agent.py)), que anexa `requestState` a `params` — e o id de
JSON-RPC é gerado por chamada (`:64`), então o retry **nunca reaproveita o id** do
request inicial.

O `requestState` é opaco para o agente: ele guarda, ecoa e nunca abre. Não aparece no
Agent Card, no artifact nem em nenhuma mensagem devolvida ao cliente A2A.

---

## 3. Decisões técnicas

**Proteção do `requestState`.** O servidor usa o `RequestStateSecurity` do SDK
([`servidor-mcp/server.py:192-198`](servidor-mcp/server.py)) com AEAD (AES-256-GCM):
selado e cifrado, não só assinado. A chave vem de `REQUEST_STATE_SECRET`
(`server.py:185`) e nunca do código — se a variável faltar, o servidor gera uma chave
efêmera e **avisa no stderr**, porque nesse modo um retry não sobrevive a um restart.
O `RequestStateBoundary` do SDK amarra o estado ao método, ao alvo e ao digest dos
argumentos, então um retry com argumentos adulterados é rejeitado em vez de tomar
efeito.

**Validade.** `ttl=600.0` — **10 minutos**, dentro da janela de 5 a 30 exigida.
Passado o prazo, o retry recebe `-32602`.

**Onde mora o estado.** O servidor MCP **não guarda nada** entre o `input_required` e o
retry: tudo que ele precisa para reconstruir o pedido viaja dentro do próprio
`requestState`. Foi medido: um retry apresentado a um processo **reiniciado** conclui a
reserva. As reservas ficam em memória do processo do servidor (`server.py:46`, lista `RESERVAS`), e o estado das Tasks fica em memória do processo do agente, num dicionário
`task_id -> Task` (`agent.py:143` declara o `request_state` de cada Task). O
`requestState` é guardado por Task, então duas Tasks pausadas ao mesmo tempo não trocam
de estado.

**Capability check manual.** O SDK não recusa sozinho: o servidor confere
`ctx.client_capabilities.elicitation.form` antes de devolver `input_required` e responde
`-32021` com `data.requiredCapabilities` quando o cliente não declarou elicitation em
form mode.

**Log no stderr.** Um middleware de contexto ([`servidor-mcp/server.py:446`](servidor-mcp/server.py))
registra **cada request recebido** com método, id de JSON-RPC, alvo e o `traceparent`
quando ele vem no `_meta`. Ele roda antes de qualquer validação, então até um request
malformado aparece no log. É por ali que se confere a propagação do trace: o trace-id
que o validador imprime na primeira linha reaparece nos `tools/call` que o agente emite.

```
[mcp] request method=tools/call id=3701dd64b274 name=reservar_sala traceparent=00-fce3d448fd8103110a2fb3c6ddaab072-8ef175829cf9b9d9-01
[mcp] request method=tools/call id=3e487fc9d40a name=reservar_sala traceparent=00-fce3d448fd8103110a2fb3c6ddaab072-8ef175829cf9b9d9-01
[mcp] request method=tools/call id=1fbb523c1085 name=reservar_sala traceparent=00-fce3d448fd8103110a2fb3c6ddaab072-8ef175829cf9b9d9-01
[mcp] request method=tools/call id=7f74f0e7b731 name=reservar_sala traceparent=00-fce3d448fd8103110a2fb3c6ddaab072-8ef175829cf9b9d9-01
```

Esse é o trace-id de `fce3d448fd8103110a2fb3c6ddaab072`, propagado do header `traceparent`
da chamada A2A até o `_meta` do request MCP.

---

## 4. Saída do validador

Última execução, com os dois processos recém-iniciados:

```
trace-id desta execucao: fce3d448fd8103110a2fb3c6ddaab072
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```

Código de saída: `0`.

---

## Contratos de uso

Pedido, em formato fixo:

```
reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>
```

Resposta à pausa: `escolha=<id da sala>` para aceitar, `escolha=recusar` para recusar.

Estados da Task: `TASK_STATE_SUBMITTED` → `TASK_STATE_WORKING` →
`TASK_STATE_COMPLETED` | `TASK_STATE_INPUT_REQUIRED` → (`COMPLETED` | `CANCELED`) |
`TASK_STATE_FAILED`. Estado terminal é definitivo.

## Estrutura

```
.
├── README.md
├── pyproject.toml          versões travadas
├── start.bat               sobe os dois processos (Windows)
├── servidor-mcp/server.py  servidor MCP, Streamable HTTP, porta 7301
├── agente/agent.py         host MCP + servidor A2A, porta 7300
├── dados/                  não alterado
├── validador/              não alterado
└── exemplos/               não alterado
```

## Tecnologias

Python 3.10+, `mcp` 2.2.0 (SDK oficial v2, revisão `2026-07-28` da spec), A2A v1.0 sobre
JSON-RPC 2.0, FastAPI 0.141.1 + Uvicorn 0.54.0 no agente, httpx 0.28.1 como cliente MCP.
Versões travadas em `pyproject.toml`. Nenhuma dependência de SDK de provedor de LLM.
