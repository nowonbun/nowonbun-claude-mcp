# nowonbun-claude-mcp

Codex와 Claude Code를 연결해서 리뷰와 질의응답에 사용할 수 있게 만든 Python 기반 브리지입니다.

현재 구조는 루트 파일 2개 중심입니다.

- `D:\work\nowonbun-claude-mcp\claude_bridge_server.py`
- `D:\work\nowonbun-claude-mcp\codex_mcp.py`

## verified 상태

- Claude MCP 연결 확인
- Codex -> Claude 전송 확인
- Claude -> reply tool -> Codex poll 응답 확인
- 실제 응답 예:
  - `안녕하세요! 무엇을 도와드릴까요?`

## 파일 설명

### `claude_bridge_server.py`

- Claude Code 쪽 stdio MCP 서버
- HTTP bridge API 제공
- Claude Code Channel notification 전송
- reply 수신 후 Codex polling 응답으로 연결

### `codex_mcp.py`

- Codex 쪽 stdio MCP 서버
- `send_to_claude`
- `check_claude_messages`

## 사전 준비

### 1) Python 의존성 설치

```bat
cd D:\work\nowonbun-claude-mcp
python -m pip install -e .
```

또는:

```bat
python -m pip install aiohttp "mcp[cli]>=1.18.0"
```

## Claude Code 설치 메모

이번 작업에서 가장 많이 막혔던 부분은 Claude CLI 설치 상태였습니다.

### native 버전 권장

공식 문서:

- https://code.claude.com/docs/en/setup
- https://code.claude.com/docs/en/getting-started

설치:

```powershell
irm https://claude.ai/install.ps1 | iex
```

기존 설치가 있다면:

```bat
claude install
```

### `claude install` 실패 예

```text
EBUSY: resource busy or locked, unlink ...\claude.exe
```

이 경우는 대체로 Claude 프로세스가 실행 중인 상태입니다.

```powershell
Get-Process | Where-Object { $_.ProcessName -like '*claude*' } | Stop-Process -Force
claude install
```

### PATH 문제

native 설치 후 아래 경로에 설치될 수 있습니다.

```text
C:\Users\nowonbun\.local\bin\claude.exe
```

사용자 PATH 추가:

```powershell
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*C:\Users\nowonbun\.local\bin*") {
    [Environment]::SetEnvironmentVariable(
        "Path",
        "C:\Users\nowonbun\.local\bin;" + $userPath,
        "User"
    )
}
```

새 터미널에서 확인:

```bat
where claude
claude --version
```

## Claude 설정

### 권장: `claude mcp add`

현재 구조에서는 루트 파일을 직접 실행하는 방식이 가장 단순합니다.

```bat
claude mcp remove nowonbun-claude-bridge
claude mcp add nowonbun-claude-bridge --env CODEX_BRIDGE_PORT=8799 -- python D:\work\nowonbun-claude-mcp\claude_bridge_server.py
```

확인:

```bat
claude mcp list
```

### `.claude.json` 메모

프로젝트 범위 MCP 상태는 아래 파일에 반영될 수 있습니다.

```text
C:\Users\nowonbun\.claude.json
```

이번 작업 중 Claude UI에 아래처럼 표시되었습니다.

```text
Local MCPs (C:\Users\nowonbun\.claude.json [project: D:\work])
```

즉, 이 프로젝트 범위 MCP 설정은 `.claude.json` 기반일 수 있습니다.

예상 예시:

```json
"nowonbun-claude-bridge": {
  "type": "stdio",
  "command": "python",
  "args": [
    "D:/work/nowonbun-claude-mcp/claude_bridge_server.py"
  ],
  "env": {
    "CODEX_BRIDGE_PORT": "8799"
  }
}
```

수동 편집보다 아래 명령을 우선 권장합니다.

```bat
claude mcp add ...
claude mcp list
claude mcp get nowonbun-claude-bridge
```

관련 문서:

- https://code.claude.com/docs/en/mcp
- https://code.claude.com/docs/en/settings

### development channels 모드로 실행

이 브리지는 Claude Code Channels 기반으로 동작합니다.

```bat
claude --dangerously-load-development-channels server:nowonbun-claude-bridge
```

경고가 나오면:

- `1. I am using this for local development`
- Enter

## Codex 설정

### 권장: `codex mcp add`

```bat
codex mcp add nowonbunClaudeBridge --env CODEX_BRIDGE_URL=http://127.0.0.1:8799 -- python D:\work\nowonbun-claude-mcp\codex_mcp.py
```

확인:

```bat
codex mcp list
```

### `~/.codex/config.toml` 예시

```toml
[mcp_servers.nowonbunClaudeBridge]
command = "python"
args = ["D:\\work\\nowonbun-claude-mcp\\codex_mcp.py"]

[mcp_servers.nowonbunClaudeBridge.env]
CODEX_BRIDGE_URL = "http://127.0.0.1:8799"
```

OpenAI Docs MCP 문서:

- https://developers.openai.com/learn/docs-mcp

## 실행 순서

### 1) Claude MCP 등록

```bat
claude mcp remove nowonbun-claude-bridge
claude mcp add nowonbun-claude-bridge --env CODEX_BRIDGE_PORT=8799 -- python D:\work\nowonbun-claude-mcp\claude_bridge_server.py
```

### 2) Claude 실행

```bat
claude --dangerously-load-development-channels server:nowonbun-claude-bridge
```

### 3) Codex MCP 등록

```bat
codex mcp add nowonbunClaudeBridge --env CODEX_BRIDGE_URL=http://127.0.0.1:8799 -- python D:\work\nowonbun-claude-mcp\codex_mcp.py
```

### 4) health 확인

```bat
curl -s http://127.0.0.1:8799/api/health
```

예상:

```json
{"status": "ok", "claude_session_attached": true}
```

## curl 테스트 방법

### 1) health

```bat
curl -s http://127.0.0.1:8799/api/health
```

### 2) Claude로 메시지 보내기

```bat
curl -s -X POST http://127.0.0.1:8799/api/from-codex -H "Content-Type: application/json" -d "{\"message\":\"안녕\"}"
```

응답 예:

```json
{"id":"codex-..."}
```

### 3) reply polling

```bat
curl -s "http://127.0.0.1:8799/api/poll-reply/받은ID?timeout=5000"
```

성공 예:

```json
{"timeout": false, "reply": "안녕하세요! 무엇을 도와드릴까요?", "failure_reason": null}
```

### 4) Claude 선제 메시지 확인

```bat
curl -s http://127.0.0.1:8799/api/pending-for-codex
```

또는 Codex 쪽 도구:

- `check_claude_messages`

## 문제 해결 메모

### 1) `failed` 로 보일 때

가장 먼저 아래를 확인합니다.

```bat
claude mcp get nowonbun-claude-bridge
```

현재 구조에서 맞는 실행 경로는 아래입니다.

```text
python D:\work\nowonbun-claude-mcp\claude_bridge_server.py
```

예전 `src` 경로나 예전 패키지 모듈 경로가 남아 있으면 실패할 수 있습니다.

### 2) 지금 구조는 `sampling/create_message` 기반이 아님

현재 구현은 Claude Code Channels 기반입니다.

- `session.create_message(...)` 를 기대하지 않음
- `notifications/claude/channel` 전송 사용

### 3) 커스텀 notification 타입 사용

`notifications/claude/channel` 은 표준 notification 타입이 아니므로 Python 구현에서는 generic notification 타입을 사용합니다.

## 검증 근거

이 문서는 아래를 근거로 정리했습니다.

- 로컬 파일:
  - `claude_bridge_server.py`
  - `codex_mcp.py`
  - `pyproject.toml`
- 실행 결과:
  - `claude mcp list`
  - `curl -s http://127.0.0.1:8799/api/health`
  - `curl -s -X POST .../api/from-codex`
  - `curl -s .../api/poll-reply/...`
- 외부 문서:
  - Claude Code setup: https://code.claude.com/docs/en/setup
  - Claude Code MCP: https://code.claude.com/docs/en/mcp
  - Claude Code settings: https://code.claude.com/docs/en/settings
  - OpenAI Docs MCP: https://developers.openai.com/learn/docs-mcp
