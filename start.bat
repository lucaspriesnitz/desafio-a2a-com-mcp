@echo off
REM Sobe o servidor MCP (7301) e o agente A2A (7300) em dois terminais.
REM
REM O REQUEST_STATE_SECRET nao vive neste arquivo: o repositorio e publico.
REM Exporte o seu antes de rodar este script:
REM
REM   for /f %%s in ('python -c "import secrets; print(secrets.token_hex(32))"') do set REQUEST_STATE_SECRET=%%s
REM
REM Sem ele, o servidor gera uma chave efemera e um requestState emitido antes de
REM um restart deixa de ser aceito depois dele.

if "%REQUEST_STATE_SECRET%"=="" (
  echo ERRO: REQUEST_STATE_SECRET nao esta definido.
  echo Gere e exporte um valor antes de rodar - o comando esta na secao
  echo "1. Como rodar" do README.md.
  exit /b 1
)

echo Iniciando servidor MCP na porta 7301...
start "MCP Server" cmd /k "cd /d %~dp0 && set REQUEST_STATE_SECRET=%REQUEST_STATE_SECRET% && python servidor-mcp/server.py"

timeout /t 3 /nobreak >nul

echo Iniciando agente A2A na porta 7300...
start "A2A Agent" cmd /k "cd /d %~dp0 && python agente/agent.py"

timeout /t 2 /nobreak >nul

echo.
echo Servidores iniciados!
echo - MCP: http://localhost:7301/mcp
echo - A2A: http://localhost:7300/a2a
echo.
echo Para rodar o validador:
echo python validador/validar.py
echo.
