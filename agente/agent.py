"""Agente A2A — Central de Salas Hill Valley Tech.

Host MCP por dentro (descobre tools, lê resource, propaga traceparent).
Servidor A2A por fora (Agent Card, SendMessage, GetTask, Task state machine).
A ponte: input_required do MCP vira TASK_STATE_INPUT_REQUIRED da Task.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
import uuid
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# --- Config ---

MCP_URL = "http://localhost:7301/mcp"
AGENT_PORT = 7300
PROTOCOLO_MCP = "2026-07-28"

# --- MCP Client ---

class MCPClient:
    """Cliente MCP simples que fala Streamable HTTP."""

    def __init__(self, url: str):
        self.url = url
        self.client = httpx.AsyncClient(timeout=30.0)
        self.tools: dict[str, dict] = {}
        self.politica_versao: str | None = None

    async def initialize(self):
        """Descobre tools e lê o resource da política."""
        # tools/list
        result = await self._call("tools/list", {})
        tools = result.get("tools", [])
        self.tools = {t["name"]: t for t in tools}

        # resources/read politica://uso
        result = await self._read_resource("politica://uso")
        contents = result.get("contents", [])
        if contents:
            text = contents[0].get("text", "")
            # Extrair versão da primeira linha
            for line in text.split("\n"):
                if line.startswith("versao:"):
                    self.politica_versao = line.replace("versao:", "").strip()
                    break

    async def _call(self, method: str, params: dict, name: str | None = None, 
                    traceparent: str | None = None,
                    input_responses: dict | None = None,
                    request_state: str | None = None) -> dict:
        """Faz uma chamada MCP."""
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": secrets.token_hex(6),
            "method": method,
            "params": {
                **params,
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": PROTOCOLO_MCP,
                    "io.modelcontextprotocol/clientCapabilities": {
                        "elicitation": {"form": {}}
                    },
                }
            }
        }
        if traceparent:
            body["params"]["_meta"]["traceparent"] = traceparent
        if input_responses is not None:
            body["params"]["inputResponses"] = input_responses
        if request_state is not None:
            body["params"]["requestState"] = request_state

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOLO_MCP,
            "Mcp-Method": method,
        }
        if name:
            headers["Mcp-Name"] = name

        resp = await self.client.post(self.url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise Exception(f"MCP error: {data['error']}")
        return data.get("result", {})

    async def _read_resource(self, uri: str, traceparent: str | None = None) -> dict:
        """Lê um resource."""
        return await self._call("resources/read", {"uri": uri}, name=uri, traceparent=traceparent)

    async def call_tool(self, name: str, arguments: dict, traceparent: str | None = None,
                        input_responses: dict | None = None,
                        request_state: str | None = None) -> dict:
        """Chama uma tool."""
        return await self._call(
            "tools/call",
            {"name": name, "arguments": arguments},
            name=name,
            traceparent=traceparent,
            input_responses=input_responses,
            request_state=request_state,
        )

    async def close(self):
        await self.client.aclose()


# --- Task State Machine ---

TASK_STATE_SUBMITTED = "TASK_STATE_SUBMITTED"
TASK_STATE_WORKING = "TASK_STATE_WORKING"
TASK_STATE_INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
TASK_STATE_COMPLETED = "TASK_STATE_COMPLETED"
TASK_STATE_CANCELED = "TASK_STATE_CANCELED"
TASK_STATE_FAILED = "TASK_STATE_FAILED"

TERMINAL_STATES = {TASK_STATE_COMPLETED, TASK_STATE_CANCELED, TASK_STATE_FAILED}


class Task:
    """Representa uma Task A2A."""

    def __init__(self, task_id: str, context_id: str):
        self.id = task_id
        self.context_id = context_id
        self.state = TASK_STATE_SUBMITTED
        self.message: dict | None = None
        self.history: list[dict] = []
        self.artifacts: list[dict] = []
        # Estado interno (não exposto ao cliente)
        self.request_state: str | None = None
        self.input_requests: dict | None = None
        self.original_arguments: dict | None = None

    def to_dict(self) -> dict:
        result = {
            "id": self.id,
            "contextId": self.context_id,
            "status": {
                "state": self.state,
            }
        }
        if self.message:
            result["status"]["message"] = self.message
        if self.history:
            result["history"] = self.history
        if self.artifacts:
            result["artifacts"] = self.artifacts
        return result


class TaskStore:
    """Armazena Tasks em memória."""

    def __init__(self):
        self.tasks: dict[str, Task] = {}

    def create(self, task_id: str, context_id: str) -> Task:
        task = Task(task_id, context_id)
        self.tasks[task_id] = task
        return task

    def get(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)


# --- Agente ---

class Agente:
    """Agente A2A que faz a ponte com o servidor MCP."""

    def __init__(self):
        self.mcp = MCPClient(MCP_URL)
        self.tasks = TaskStore()

    async def initialize(self):
        await self.mcp.initialize()

    async def handle_send_message(self, message: dict, traceparent: str | None = None) -> dict:
        """Processa um SendMessage."""
        task_id = message.get("taskId")
        text = ""
        for part in message.get("parts", []):
            if part.get("type") == "text" or "text" in part:
                text = part.get("text", "")
                break

        if task_id:
            # Continuação de uma Task existente
            task = self.tasks.get(task_id)
            if not task:
                return {"error": {"code": -32001, "message": "Task não encontrada"}}
            if task.state in TERMINAL_STATES:
                return {"error": {"code": -32002, "message": "Task já está em estado terminal"}}
            return await self._continue_task(task, text, traceparent)
        else:
            # Nova Task
            new_task_id = f"task-{secrets.token_hex(6)}"
            context_id = f"ctx-{secrets.token_hex(6)}"
            task = self.tasks.create(new_task_id, context_id)
            return await self._start_task(task, text, message, traceparent)

    async def _start_task(self, task: Task, text: str, original_message: dict, traceparent: str | None = None) -> dict:
        """Inicia uma nova Task."""
        task.state = TASK_STATE_WORKING

        # Adicionar mensagem do usuário ao histórico
        user_msg = {
            "messageId": original_message.get("messageId", f"msg-{secrets.token_hex(6)}"),
            "role": "ROLE_USER",
            "parts": [{"text": text}],
        }
        task.history.append(user_msg)

        # Parsear o pedido
        # Formato: reservar sala=<id> inicio=<iso> fim=<iso> responsavel=<nome>
        args = self._parse_pedido(text)
        if not args:
            task.state = TASK_STATE_FAILED
            task.message = {
                "messageId": f"msg-{secrets.token_hex(6)}",
                "role": "ROLE_AGENT",
                "parts": [{"text": "Pedido inválido. Use: reservar sala=<id> inicio=<iso> fim=<iso> responsavel=<nome>"}],
                "taskId": task.id,
                "contextId": task.context_id,
            }
            task.history.append(task.message)
            return {"task": task.to_dict()}

        task.original_arguments = args

        # Chamar a tool MCP
        try:
            result = await self.mcp.call_tool("reservar_sala", args, traceparent=traceparent)
        except Exception as e:
            task.state = TASK_STATE_FAILED
            task.message = {
                "messageId": f"msg-{secrets.token_hex(6)}",
                "role": "ROLE_AGENT",
                "parts": [{"text": str(e)}],
                "taskId": task.id,
                "contextId": task.context_id,
            }
            task.history.append(task.message)
            return {"task": task.to_dict()}

        # Verificar o resultado
        result_type = result.get("resultType", "complete")

        if result_type == "input_required":
            # Pausar a Task
            task.state = TASK_STATE_INPUT_REQUIRED
            task.input_requests = result.get("inputRequests", {})
            task.request_state = result.get("requestState")

            # Extrair alternativas do schema
            alternativas = self._extrair_alternativas(task.input_requests)
            alternativas_str = ", ".join(alternativas)

            task.message = {
                "messageId": f"msg-{secrets.token_hex(6)}",
                "role": "ROLE_AGENT",
                "parts": [{"text": f"alternativas: {alternativas_str}"}],
                "taskId": task.id,
                "contextId": task.context_id,
            }
            task.history.append(task.message)
            return {"task": task.to_dict()}

        elif result_type == "complete":
            # Verificar se é erro
            if result.get("isError"):
                task.state = TASK_STATE_FAILED
                error_text = self._extrair_texto(result)
                task.message = {
                    "messageId": f"msg-{secrets.token_hex(6)}",
                    "role": "ROLE_AGENT",
                    "parts": [{"text": error_text}],
                    "taskId": task.id,
                    "contextId": task.context_id,
                }
                task.history.append(task.message)
                return {"task": task.to_dict()}

            # Sucesso
            task.state = TASK_STATE_COMPLETED
            structured = result.get("structuredContent", {})
            artifact = {
                "artifactId": f"art-{secrets.token_hex(6)}",
                "name": "reserva",
                "parts": [{"text": json.dumps(structured)}],
            }
            task.artifacts.append(artifact)
            task.message = {
                "messageId": f"msg-{secrets.token_hex(6)}",
                "role": "ROLE_AGENT",
                "parts": [{"text": f"Reserva {structured.get('reserva')} confirmada na {structured.get('sala')}."}],
                "taskId": task.id,
                "contextId": task.context_id,
            }
            task.history.append(task.message)
            return {"task": task.to_dict()}

        else:
            task.state = TASK_STATE_FAILED
            task.message = {
                "messageId": f"msg-{secrets.token_hex(6)}",
                "role": "ROLE_AGENT",
                "parts": [{"text": f"Resultado inesperado: {result_type}"}],
                "taskId": task.id,
                "contextId": task.context_id,
            }
            task.history.append(task.message)
            return {"task": task.to_dict()}

    async def _continue_task(self, task: Task, text: str, traceparent: str | None = None) -> dict:
        """Continua uma Task pausada."""
        # Parsear a escolha
        # Formato: escolha=<sala> ou escolha=recusar
        if not text.startswith("escolha="):
            # Manter pausada
            return {"task": task.to_dict()}

        escolha = text.replace("escolha=", "").strip()

        # Verificar se a escolha é válida
        alternativas = self._extrair_alternativas(task.input_requests)
        if escolha != "recusar" and escolha not in alternativas:
            # Escolha inválida, manter pausada
            return {"task": task.to_dict()}

        # Preparar o retry
        if escolha == "recusar":
            input_responses = {
                "escolha_de_sala": {"action": "decline"}
            }
        else:
            input_responses = {
                "escolha_de_sala": {"action": "accept", "content": {"sala": escolha}}
            }

        # Chamar a tool MCP com o retry
        try:
            result = await self.mcp.call_tool(
                "reservar_sala",
                task.original_arguments,
                traceparent=traceparent,
                input_responses=input_responses,
                request_state=task.request_state,
            )
        except Exception as e:
            task.state = TASK_STATE_FAILED
            task.message = {
                "messageId": f"msg-{secrets.token_hex(6)}",
                "role": "ROLE_AGENT",
                "parts": [{"text": str(e)}],
                "taskId": task.id,
                "contextId": task.context_id,
            }
            task.history.append(task.message)
            return {"task": task.to_dict()}

        # Verificar o resultado
        result_type = result.get("resultType", "complete")

        if result_type == "complete":
            if result.get("isError"):
                task.state = TASK_STATE_FAILED
                error_text = self._extrair_texto(result)
                task.message = {
                    "messageId": f"msg-{secrets.token_hex(6)}",
                    "role": "ROLE_AGENT",
                    "parts": [{"text": error_text}],
                    "taskId": task.id,
                    "contextId": task.context_id,
                }
                task.history.append(task.message)
                return {"task": task.to_dict()}

            structured = result.get("structuredContent", {})

            if escolha == "recusar":
                task.state = TASK_STATE_CANCELED
                task.message = {
                    "messageId": f"msg-{secrets.token_hex(6)}",
                    "role": "ROLE_AGENT",
                    "parts": [{"text": "Reserva recusada."}],
                    "taskId": task.id,
                    "contextId": task.context_id,
                }
                task.history.append(task.message)
                return {"task": task.to_dict()}

            # Sucesso
            task.state = TASK_STATE_COMPLETED
            artifact = {
                "artifactId": f"art-{secrets.token_hex(6)}",
                "name": "reserva",
                "parts": [{"text": json.dumps(structured)}],
            }
            task.artifacts.append(artifact)
            task.message = {
                "messageId": f"msg-{secrets.token_hex(6)}",
                "role": "ROLE_AGENT",
                "parts": [{"text": f"Reserva {structured.get('reserva')} confirmada na {structured.get('sala')}."}],
                "taskId": task.id,
                "contextId": task.context_id,
            }
            task.history.append(task.message)
            return {"task": task.to_dict()}

        elif result_type == "input_required":
            # Ainda precisa de input (escolha inválida?)
            task.input_requests = result.get("inputRequests", {})
            task.request_state = result.get("requestState")
            alternativas = self._extrair_alternativas(task.input_requests)
            alternativas_str = ", ".join(alternativas)
            task.message = {
                "messageId": f"msg-{secrets.token_hex(6)}",
                "role": "ROLE_AGENT",
                "parts": [{"text": f"alternativas: {alternativas_str}"}],
                "taskId": task.id,
                "contextId": task.context_id,
            }
            task.history.append(task.message)
            return {"task": task.to_dict()}

        else:
            task.state = TASK_STATE_FAILED
            task.message = {
                "messageId": f"msg-{secrets.token_hex(6)}",
                "role": "ROLE_AGENT",
                "parts": [{"text": f"Resultado inesperado: {result_type}"}],
                "taskId": task.id,
                "contextId": task.context_id,
            }
            task.history.append(task.message)
            return {"task": task.to_dict()}

    def _parse_pedido(self, text: str) -> dict | None:
        """Parseia o pedido no formato fixo."""
        # Formato: reservar sala=<id> inicio=<iso> fim=<iso> responsavel=<nome>
        if not text.startswith("reservar "):
            return None

        parts = text.replace("reservar ", "").split()
        args = {}
        for part in parts:
            if "=" in part:
                key, value = part.split("=", 1)
                args[key] = value

        required = ["sala", "inicio", "fim", "responsavel"]
        if not all(k in args for k in required):
            return None

        return args

    def _extrair_alternativas(self, input_requests: dict | None) -> list[str]:
        """Extrai as alternativas do inputRequests."""
        if not input_requests:
            return []
        for key, req in input_requests.items():
            params = req.get("params", {})
            schema = params.get("requestedSchema", {})
            props = schema.get("properties", {})
            sala_prop = props.get("sala", {})
            enum = sala_prop.get("enum", [])
            if enum:
                return enum
            # Se não tem enum, talvez tenha const
            const = sala_prop.get("const")
            if const:
                return [const]
        return []

    def _extrair_texto(self, result: dict) -> str:
        """Extrai o texto do resultado."""
        content = result.get("content", [])
        if content:
            return content[0].get("text", "")
        return ""

    async def handle_get_task(self, task_id: str) -> dict:
        """Processa um GetTask."""
        task = self.tasks.get(task_id)
        if not task:
            return {"error": {"code": -32001, "message": "Task não encontrada"}}
        return {"task": task.to_dict()}


# --- FastAPI App ---

app = FastAPI()
agente = Agente()


@app.on_event("startup")
async def startup():
    await agente.initialize()


@app.get("/.well-known/agent-card.json")
async def agent_card():
    """Agent Card no well-known URI."""
    return {
        "name": "Central de Salas",
        "description": "Reserva salas de reuniao da Hill Valley Tech.",
        "provider": {
            "organization": "Hill Valley Tech",
            "url": "https://hillvalley.example"
        },
        "version": "1.0.0",
        "supportedInterfaces": [
            {
                "url": f"http://127.0.0.1:{AGENT_PORT}/a2a",
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0"
            }
        ],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extendedAgentCard": False
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "reservar-sala",
                "name": "Reservar sala",
                "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                "tags": ["salas", "agenda"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ]
            }
        ]
    }


@app.post("/a2a")
async def a2a_endpoint(request: Request):
    """Endpoint JSON-RPC do A2A."""
    body = await request.json()
    method = body.get("method")
    params = body.get("params", {})

    # Propagar traceparent se presente
    traceparent = request.headers.get("traceparent")

    if method == "SendMessage":
        message = params.get("message", {})
        result = await agente.handle_send_message(message, traceparent)
        # Verificar se é um erro
        if "error" in result:
            return {"jsonrpc": "2.0", "id": body.get("id"), "error": result["error"]}
        return {"jsonrpc": "2.0", "id": body.get("id"), "result": result}

    elif method == "GetTask":
        task_id = params.get("id")
        result = await agente.handle_get_task(task_id)
        # Verificar se é um erro
        if "error" in result:
            return {"jsonrpc": "2.0", "id": body.get("id"), "error": result["error"]}
        return {"jsonrpc": "2.0", "id": body.get("id"), "result": result}

    else:
        return {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32601, "message": "Method not found"}}


def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=AGENT_PORT)


if __name__ == "__main__":
    main()
