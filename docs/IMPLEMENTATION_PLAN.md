# Project Automatron: Технічний План Реалізації v1.0

> Дата: 2026-02-19  
> Статус: DRAFT  
> Автор: GitHub Copilot + Human  

---

## Зміст

1. [Фінальна Архітектура та Рішення](#1-фінальна-архітектура-та-рішення)
2. [Структура Репозиторію](#2-структура-репозиторію)
3. [Фаза 1: Scaffolding та Інфраструктура](#фаза-1-scaffolding-та-інфраструктура)
4. [Фаза 2: Orchestrator Core (LangGraph)](#фаза-2-orchestrator-core-langgraph-state-machine)
5. [Фаза 3: Architect Module](#фаза-3-architect-module-llm-integration)
6. [Фаза 4: Builder Module (Cline Integration)](#фаза-4-builder-module-cline-cli-integration)
7. [Фаза 5: Docker Sandbox Engine](#фаза-5-docker-sandbox-engine)
8. [Фаза 6: Web UI (Next.js)](#фаза-6-web-ui-nextjs)
9. [Фаза 7: Інтеграція та E2E Flow](#фаза-7-інтеграція-та-end-to-end-flow)
10. [Фаза 8: Hardening, Тестування, Deploy](#фаза-8-hardening-тестування-deploy)
11. [Верифікація](#верифікація)
12. [Прийняті Рішення](#прийняті-рішення)
13. [Ризики та Мітигація](#ризики-та-мітигація)
14. [Часова Оцінка](#часова-оцінка-summary)

---

## 1. Фінальна Архітектура та Рішення

### Стек технологій

| Компонент        | Технологія                                    |
|------------------|-----------------------------------------------|
| Orchestrator     | Python 3.12 + LangGraph 1.x + langgraph-checkpoint-sqlite |
| Architect LLM    | Model-agnostic (Claude Opus / GPT-5.3 / Gemini 3 Pro via litellm) |
| Builder          | Cline CLI 2.x (`npm i -g cline`, headless `-y` mode) |
| Web UI           | Next.js 15 + Tailwind CSS + shadcn/ui         |
| Web UI ↔ Backend | WebSocket (Socket.IO) + REST API              |
| State DB         | SQLite (LangGraph SqliteSaver)                |
| Project DB       | SQLite (проекти, сесії, логи)                 |
| Secrets          | Docker Secrets                                |
| Containers       | Docker SDK for Python (docker-py)             |
| Base Image       | Ubuntu 24.04 LTS (custom "Golden Image")      |
| Notifications    | Web UI only (WebSocket push)                  |
| Auth             | Без аутентифікації (private network / VPN)    |
| Deploy target    | Виділений Linux VPS                           |

### Діаграма потоку даних

```
Human ←→ [Web UI (Next.js)] ←WebSocket→ [API Gateway (FastAPI)]
                                              ↓
                                    [Orchestrator (LangGraph)]
                                      ↙              ↘
              [Architect Node]              [Builder Node]
              (LLM API call)                (Docker + Cline CLI)
                    ↓                              ↓
              PLAN.md + STACK_CONFIG.json    Code in /workspace
```

### Комунікаційний протокол

```
Web UI ←→ FastAPI (HTTP + WebSocket)
FastAPI ←→ LangGraph (in-process Python call)
LangGraph Architect Node ←→ LLM Provider (HTTP via litellm)
LangGraph Builder Node ←→ Docker Container (docker-py SDK)
Docker Container ←→ Cline CLI (subprocess inside container)
```

---

## 2. Структура Репозиторію

```
project-automatron/
├── README.md
├── docker-compose.yml              # Розгортання всього стека
├── .env.example                    # Шаблон змінних оточення
├── Makefile                        # Команди dev/build/deploy
│
├── orchestrator/                   # Python backend (FastAPI + LangGraph)
│   ├── pyproject.toml              # Poetry/uv проект
│   ├── orchestrator/
│   │   ├── __init__.py
│   │   ├── main.py                 # FastAPI app entry point
│   │   ├── config.py               # Pydantic Settings
│   │   ├── api/
│   │   │   ├── __init__.py
│   │   │   ├── routes.py           # REST endpoints
│   │   │   └── websocket.py        # WebSocket handler
│   │   ├── graph/
│   │   │   ├── __init__.py
│   │   │   ├── state.py            # LangGraph State schema (TypedDict)
│   │   │   ├── graph.py            # StateGraph definition
│   │   │   ├── nodes/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── architect.py    # Architect node (LLM call)
│   │   │   │   ├── builder.py      # Builder node (Docker + Cline)
│   │   │   │   ├── reviewer.py     # Status classifier node
│   │   │   │   └── scaffold.py     # Init/scaffolding node
│   │   │   └── edges.py            # Conditional edge functions
│   │   ├── docker_engine/
│   │   │   ├── __init__.py
│   │   │   ├── manager.py          # Docker container lifecycle
│   │   │   ├── image_builder.py    # Golden Image builder
│   │   │   └── port_allocator.py   # Dynamic port mapping
│   │   ├── plan_parser/
│   │   │   ├── __init__.py
│   │   │   ├── parser.py           # PLAN.md parser (frontmatter + checkboxes)
│   │   │   └── writer.py           # PLAN.md writer (mark [x], update)
│   │   ├── llm/
│   │   │   ├── __init__.py
│   │   │   ├── provider.py         # litellm wrapper (model-agnostic)
│   │   │   └── prompts.py          # Prompt loader from files
│   │   ├── secrets/
│   │   │   ├── __init__.py
│   │   │   └── manager.py          # Docker Secrets reader
│   │   └── models/
│   │       ├── __init__.py
│   │       ├── project.py          # Project SQLite models
│   │       └── session.py          # Session/task models
│   ├── prompts/
│   │   ├── architect_v1.txt        # Architect system prompt
│   │   ├── builder_v1.txt          # Builder task prompt template
│   │   └── reviewer_v1.txt         # Status classifier prompt
│   ├── scripts/
│   │   ├── init-nextjs.sh          # Scaffold: Next.js
│   │   ├── init-react-vite.sh      # Scaffold: React + Vite
│   │   ├── init-python.sh          # Scaffold: Python project
│   │   └── init-generic.sh         # Scaffold: generic fallback
│   └── tests/
│       ├── test_graph.py
│       ├── test_plan_parser.py
│       ├── test_docker_engine.py
│       └── test_architect.py
│
├── web-ui/                         # Next.js frontend
│   ├── package.json
│   ├── next.config.js
│   ├── tailwind.config.ts
│   ├── src/
│   │   ├── app/
│   │   │   ├── layout.tsx
│   │   │   ├── page.tsx            # Dashboard: список проектів
│   │   │   └── project/
│   │   │       └── [id]/
│   │   │           ├── page.tsx    # Project view: chat + plan + status
│   │   │           ├── plan/
│   │   │           │   └── page.tsx # PLAN.md editor
│   │   │           └── logs/
│   │   │               └── page.tsx # Build logs viewer
│   │   ├── components/
│   │   │   ├── chat/
│   │   │   │   ├── ChatPanel.tsx    # Architect conversation
│   │   │   │   ├── MessageBubble.tsx
│   │   │   │   └── ChatInput.tsx
│   │   │   ├── plan/
│   │   │   │   ├── PlanEditor.tsx   # Markdown editor for PLAN.md
│   │   │   │   ├── PlanPreview.tsx  # Rendered preview з progress
│   │   │   │   └── ApproveButton.tsx
│   │   │   ├── status/
│   │   │   │   ├── StatusBadge.tsx  # SUCCESS/BLOCKER/AMBIGUITY badges
│   │   │   │   ├── ProgressBar.tsx
│   │   │   │   └── AlertPanel.tsx   # Human Intervention alerts
│   │   │   ├── logs/
│   │   │   │   └── LogStream.tsx    # Real-time build log viewer
│   │   │   └── layout/
│   │   │       ├── Sidebar.tsx
│   │   │       └── Header.tsx
│   │   ├── hooks/
│   │   │   ├── useWebSocket.ts     # WebSocket connection hook
│   │   │   ├── useProject.ts
│   │   │   └── useChat.ts
│   │   ├── lib/
│   │   │   ├── api.ts              # REST API client
│   │   │   ├── socket.ts           # Socket.IO client
│   │   │   └── types.ts            # Shared TypeScript types
│   │   └── stores/
│   │       └── projectStore.ts     # Zustand store
│   └── public/
│
├── docker/
│   ├── golden-image/
│   │   └── Dockerfile              # "Золотий Образ" — Ubuntu 24.04 + runtimes
│   ├── orchestrator/
│   │   └── Dockerfile              # Orchestrator container
│   └── web-ui/
│       └── Dockerfile              # Next.js production container
│
└── docs/
    ├── ARCHITECTURE.md             # Цей документ (конституція)
    └── DEPLOYMENT.md               # Інструкція розгортання на VPS
```

---

## Фаза 1: Scaffolding та Інфраструктура

**Estimated: 2-3 дні**

### 1.1. Ініціалізація репозиторію та структури
- Створити структуру каталогів як описано вище
- Ініціалізувати Git з `.gitignore` (Python, Node.js, Docker, `.env`)
- Створити `README.md` з описом проекту

### 1.2. Python Backend Setup
- Ініціалізувати `orchestrator/pyproject.toml` (використати `uv` або `poetry`)
- Залежності:
  ```
  fastapi>=0.115
  uvicorn[standard]
  langgraph>=1.0.9
  langgraph-checkpoint-sqlite>=3.0
  langchain-core
  litellm>=1.55
  docker>=7.0
  python-socketio>=5.11
  pydantic>=2.0
  pydantic-settings
  python-frontmatter
  aiosqlite
  httpx
  pytest
  pytest-asyncio
  ```
- Налаштувати `ruff` linter + `mypy` type checking

### 1.3. Next.js Frontend Setup  
- `npx create-next-app@latest web-ui` з TypeScript + Tailwind + App Router
- Додати залежності: `socket.io-client`, `zustand`, `@uiw/react-md-editor` (markdown editor), `lucide-react`, `shadcn/ui`
- Налаштувати ESLint + Prettier

### 1.4. Docker Golden Image
- Створити `docker/golden-image/Dockerfile`:
  - Base: `ubuntu:24.04`
  - Install: `python3.12`, `python3-venv`, `python3-pip`, `nodejs 22`, `npm`, `pnpm`, `git`, `curl`, `wget`, `build-essential`, `jq`
  - `npm i -g cline` (глобальна установка Cline CLI)
  - Створити користувача `developer` (uid=1000)
  - `WORKDIR /workspace`
  - `USER developer`
- Зібрати і протестувати image: `docker build -t automatron/golden:latest .`

### 1.5. Docker Compose для розробки
- `docker-compose.yml` з сервісами:
  - `orchestrator` (Python FastAPI, port 8000)
  - `web-ui` (Next.js dev, port 3000)
  - Volumes для hot-reload

### 1.6. Файл `.env.example`
```
# LLM Providers
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=

# Architect config
ARCHITECT_MODEL=claude-opus-4-20250918
ARCHITECT_PROMPT_VERSION=v1

# Builder config  
BUILDER_MODEL=gpt-5.3-codex
BUILDER_CLINE_TIMEOUT=300

# Docker
GOLDEN_IMAGE=automatron/golden:latest
WORKSPACE_BASE_PATH=/var/automatron/workspaces
PORT_RANGE_START=7000
PORT_RANGE_END=7999

# Database
SQLITE_DB_PATH=./data/automatron.db
CHECKPOINT_DB_PATH=./data/checkpoints.db
```

---

## Фаза 2: Orchestrator Core (LangGraph State Machine)

**Estimated: 5-7 днів**

### 2.1. State Schema Definition

**Файл:** `orchestrator/orchestrator/graph/state.py`

Визначити `TypedDict` для глобального стану графа:

```python
from typing import Annotated
from typing_extensions import TypedDict
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

class AutomatronState(TypedDict):
    # Project metadata
    project_id: str
    project_name: str
    
    # PLAN.md content
    plan_md: str                              # raw PLAN.md content
    stack_config: dict                        # parsed STACK_CONFIG.json
    
    # Current execution
    current_task_index: int                   # index of current [ ] task
    current_task_text: str                    # text of current task
    total_tasks: int
    completed_tasks: int
    
    # Messages (Architect chat history)
    messages: Annotated[list[AnyMessage], add_messages]
    
    # Builder output
    builder_status: str                       # SUCCESS|BLOCKER|AMBIGUITY|SILENT_DECISION
    builder_output: str                       # stdout/stderr from Cline
    builder_error_detail: str                 # error description for escalation
    
    # Anti-loop tracking
    escalation_count: int                     # per-task counter
    escalation_history: list[dict]            # {task_index, status, timestamp}
    
    # Docker
    container_id: str
    container_port: int
    
    # Phase tracking
    phase: str                                # PLANNING|SCAFFOLDING|EXECUTING|FROZEN|COMPLETED
    
    # Human intervention
    requires_human: bool
    human_intervention_reason: str
```

### 2.2. Graph Definition (Nodes + Edges)

**Файл:** `orchestrator/orchestrator/graph/graph.py`

**Nodes:**
1. `architect_node` — Викликає LLM для планування, генерує/оновлює PLAN.md
2. `human_review_node` — `interrupt()` для Human-in-the-Loop (approve PLAN.md)
3. `scaffold_node` — Піднімає Docker container, запускає init script
4. `task_selector_node` — Парсить PLAN.md, знаходить перший `[ ]`
5. `builder_node` — Запускає Cline CLI в контейнері з поточною задачею
6. `status_classifier_node` — Класифікує output Cline в один з 4 статусів
7. `escalation_node` — Обробляє BLOCKER/AMBIGUITY (відправляє на Architect)
8. `completion_node` — Фіналізація проекту

**Edges (Conditional routing):**
```
START → architect_node
architect_node → human_review_node
human_review_node → scaffold_node (after interrupt resume)
scaffold_node → task_selector_node

task_selector_node →
  if no tasks left → completion_node
  else → builder_node

builder_node → status_classifier_node

status_classifier_node →
  SUCCESS → task_selector_node (loop)
  SILENT_DECISION → task_selector_node (loop)
  BLOCKER → escalation_check_node
  AMBIGUITY → escalation_check_node

escalation_check_node →
  if escalation_count > 2 → freeze_node (interrupt + human alert)
  else → architect_node (re-plan)

architect_node (re-plan) → builder_node (retry same task)

freeze_node → human_review_node (interrupt)
completion_node → END
```

### 2.3. Checkpoint Configuration

**Файл:** `orchestrator/orchestrator/graph/graph.py`

- Використати `SqliteSaver` з `langgraph-checkpoint-sqlite`
- Файл БД: `CHECKPOINT_DB_PATH` з конфігурації
- Кожен проект = окремий `thread_id` (формат: `project_{project_id}`)
- Time-Travel: endpoint `/api/project/{id}/history` повертає список checkpoints
- Rollback: endpoint `/api/project/{id}/rollback/{checkpoint_id}` — `graph.update_state()`

```python
from langgraph.checkpoint.sqlite import SqliteSaver

with SqliteSaver.from_conn_string("checkpoint.db") as checkpointer:
    graph = workflow.compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": f"project_{project_id}"}}
    graph.invoke(inputs, config)
```

### 2.4. PLAN.md Parser & Writer

**Файл:** `orchestrator/orchestrator/plan_parser/parser.py`

Функціональність:
- `parse_plan(content: str) -> PlanData` — парсинг frontmatter (YAML) + body
- `get_next_task(content: str) -> Task | None` — знайти перший `- [ ]`
- `mark_task_complete(content: str, task_index: int) -> str` — замінити `[ ]` → `[x]`
- `get_progress(content: str) -> Progress` — підрахунок `[x]` vs `[ ]`
- `get_global_rules(content: str) -> list[str]` — витягти `global_rules` з frontmatter

**Формат Task:**
```python
from dataclasses import dataclass

@dataclass
class Task:
    index: int
    phase: str           # "Фаза 1", "Фаза 2"...
    title: str           # "Setup Database"
    description: str     # Full text including *Context* blocks
    line_number: int     # for precise editing

@dataclass
class Progress:
    completed: int
    total: int
    percentage: float
```

### 2.5. FastAPI Application

**Файл:** `orchestrator/orchestrator/main.py`

**REST Endpoints:**
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/projects` | Створити новий проект |
| `GET` | `/api/projects` | Список проектів |
| `GET` | `/api/projects/{id}` | Деталі проекту |
| `POST` | `/api/projects/{id}/start` | Запустити Architect flow |
| `POST` | `/api/projects/{id}/approve` | Approve PLAN.md (resume interrupt) |
| `POST` | `/api/projects/{id}/stop` | Зупинити виконання |
| `GET` | `/api/projects/{id}/plan` | Отримати PLAN.md |
| `PUT` | `/api/projects/{id}/plan` | Оновити PLAN.md вручну |
| `GET` | `/api/projects/{id}/history` | Список checkpoints (Time-Travel) |
| `POST` | `/api/projects/{id}/rollback/{checkpoint_id}` | Відкат |
| `GET` | `/api/projects/{id}/logs` | Build logs |
| `GET` | `/api/projects/{id}/preview-url` | URL для Live Preview |

**WebSocket Events (Socket.IO):**

| Direction | Event | Payload |
|-----------|-------|---------|
| Server → Client | `architect:message` | Повідомлення від Architect (streaming) |
| Server → Client | `builder:log` | Real-time лог від Cline |
| Server → Client | `status:update` | Зміна статусу (phase, progress, alerts) |
| Server → Client | `human:required` | Сповіщення про необхідність втручання |
| Server → Client | `plan:updated` | PLAN.md оновлено |
| Client → Server | `chat:message` | Повідомлення від користувача до Architect |

### 2.6. Project SQLite Database

**Файл:** `orchestrator/orchestrator/models/project.py`

Таблиці (aiosqlite, raw SQL або sqlmodel):

**`projects`**
| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | UUID |
| name | TEXT | Назва проекту |
| status | TEXT | PLANNING/SCAFFOLDING/EXECUTING/FROZEN/COMPLETED |
| plan_md | TEXT | Вміст PLAN.md |
| stack_config_json | TEXT | JSON STACK_CONFIG |
| container_id | TEXT | Docker container ID |
| port | INTEGER | Allocated external port |
| created_at | TIMESTAMP | |
| updated_at | TIMESTAMP | |

**`sessions`**
| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | UUID |
| project_id | TEXT FK | |
| thread_id | TEXT | LangGraph thread ID |
| phase | TEXT | |
| started_at | TIMESTAMP | |
| ended_at | TIMESTAMP | |

**`task_logs`**
| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | UUID |
| session_id | TEXT FK | |
| task_index | INTEGER | |
| task_text | TEXT | |
| status | TEXT | SUCCESS/BLOCKER/AMBIGUITY/SILENT_DECISION |
| cline_output | TEXT | |
| duration_s | REAL | |
| created_at | TIMESTAMP | |

**`chat_messages`**
| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | UUID |
| project_id | TEXT FK | |
| role | TEXT | human/architect |
| content | TEXT | |
| created_at | TIMESTAMP | |

---

## Фаза 3: Architect Module (LLM Integration)

**Estimated: 3-4 дні**

### 3.1. LLM Provider Abstraction (litellm)

**Файл:** `orchestrator/orchestrator/llm/provider.py`

- Обгортка навколо `litellm.acompletion()` для model-agnostic виклику
- Конфігурація моделі через `ARCHITECT_MODEL` env var
- Підтримка streaming (для real-time chat у Web UI)
- Fallback: якщо primary model fail → спробувати secondary model
- Логування: token usage, latency, cost

```python
import litellm

async def call_architect(messages: list[dict], model: str = None) -> AsyncGenerator[str, None]:
    model = model or settings.ARCHITECT_MODEL
    response = await litellm.acompletion(
        model=model,
        messages=messages,
        stream=True,
    )
    async for chunk in response:
        content = chunk.choices[0].delta.content
        if content:
            yield content
```

### 3.2. Prompt Management

**Файл:** `orchestrator/orchestrator/llm/prompts.py` + `orchestrator/prompts/`

**System prompt для Architect (`prompts/architect_v1.txt`):**
- Роль: "Ти — технічний архітектор. Твоя задача — перетворити абстрактну ідею в конкретний PLAN.md"
- Формат виходу: Markdown з frontmatter (YAML), checkboxes, Context blocks
- Правила: strict TypeScript, no any, конкретні бібліотеки, версії
- Вимога: генерувати `STACK_CONFIG.json` окремо
  
**Prompt для Builder task injection (`prompts/builder_v1.txt`):**
- Template: "Ти — кодер. Виконай ТІЛЬКИ цю задачу: {task}. Global rules: {rules}. Не змінюй архітектуру."
  
**Prompt для Status Classifier (`prompts/reviewer_v1.txt`):**
- Аналізує output Cline і класифікує: SUCCESS / BLOCKER / AMBIGUITY / SILENT_DECISION
- Повертає structured JSON: `{"status": "BLOCKER", "reason": "...", "suggestion": "..."}`

### 3.3. Architect Node Implementation

**Файл:** `orchestrator/orchestrator/graph/nodes/architect.py`

Два режими роботи:

1. **Initial Planning** — отримує user prompt, веде діалог, генерує PLAN.md
2. **Re-planning (Escalation)** — отримує BLOCKER/AMBIGUITY, аналізує помилку, оновлює задачу в PLAN.md

Architect отримує:
- Історію чату (`state["messages"]`)
- Поточний PLAN.md (якщо є)
- Builder error (при ескалації)

Architect повертає:
- Оновлений `plan_md`
- Оновлений `stack_config`
- Нові повідомлення в `messages`

### 3.4. STACK_CONFIG.json Generator

Architect генерує JSON артефакт з метаданими:

```json
{
  "stack": "nextjs_tailwind",
  "framework": "Next.js 15",
  "styling": "Tailwind CSS",
  "db": "postgres",
  "orm": "drizzle",
  "auth": "next-auth",
  "port": 3000,
  "package_manager": "pnpm",
  "init_script": "init-nextjs.sh"
}
```

---

## Фаза 4: Builder Module (Cline CLI Integration)

**Estimated: 4-5 днів**

### 4.1. Builder Node Implementation

**Файл:** `orchestrator/orchestrator/graph/nodes/builder.py`

Послідовність:
1. Прочитати `state["current_task_text"]` та `state["plan_md"]` frontmatter (global_rules)
2. Сформувати prompt для Cline: inject task + context + rules
3. Виконати `docker exec` в контейнері проекту:
   ```bash
   cline -y -m {model} --timeout {timeout} "{prompt}"
   ```
4. Захопити stdout/stderr в реальному часі → стрімити через WebSocket
5. Після завершення — прочитати оновлений PLAN.md з контейнера (Cline може сам ставити `[x]`)
6. Повернути output для класифікації

### 4.2. Cline CLI Configuration inside Container

- Auth: `cline auth -p {provider} -k {api_key} -m {model}` — виконується при scaffold
- Working directory: `/workspace` (mount point)
- PLAN.md копіюється в `/workspace/PLAN.md` перед кожною задачею
- `.clinerules` файл в `/workspace/.clinerules/` — глобальні правила для Cline:
  - Не змінювати архітектуру
  - Не видаляти існуючі файли без явної інструкції
  - Дотримуватися global_rules з PLAN.md

### 4.3. Status Classifier Node

**Файл:** `orchestrator/orchestrator/graph/nodes/reviewer.py`

- Отримує raw output від Cline (stdout + stderr + exit code)
- Викликає LLM (lightweight model, наприклад gpt-4.1-mini) для класифікації
- Повертає один з 4 статусів + причину
- **Швидкий шлях:** Exit code 0 + no errors → SUCCESS (без LLM виклику)
- **Повна класифікація:** Exit code != 0 → обов'язкова LLM класифікація

Структура відповіді:
```python
@dataclass
class ClassificationResult:
    status: Literal["SUCCESS", "BLOCKER", "AMBIGUITY", "SILENT_DECISION"]
    reason: str
    suggestion: str | None  # для BLOCKER/AMBIGUITY — рекомендація для Architect
```

### 4.4. Anti-Loop Protection

**Файл:** `orchestrator/orchestrator/graph/edges.py`

- Лічильник `escalation_count` інкрементується при BLOCKER/AMBIGUITY
- Скидається при переході до нової задачі (SUCCESS)
- Якщо `escalation_count > 2`:
  - `state["phase"] = "FROZEN"`
  - `state["requires_human"] = True`
  - `state["human_intervention_reason"] = "Anti-Loop: task #{index} failed 3 times"`
  - Виконується `interrupt()` → граф зупиняється
  - Web UI показує alert

### 4.5. PLAN.md Sync Strategy

- Orchestrator зберігає canonical PLAN.md в SQLite (`projects.plan_md`)
- **Перед кожною задачею:** копіювати PLAN.md в контейнер (`docker cp`)
- **Після кожної задачі:** прочитати PLAN.md з контейнера (`docker exec cat /workspace/PLAN.md`)
- Якщо Cline модифікував чекбокси — синхронізувати в SQLite
- Конфліктів не буває: тільки один writer (Cline) одночасно

---

## Фаза 5: Docker Sandbox Engine

**Estimated: 3-4 дні**

### 5.1. Container Manager

**Файл:** `orchestrator/orchestrator/docker_engine/manager.py`

API:

```python
class ContainerManager:
    async def create_project_container(
        self, project_id: str, stack_config: dict
    ) -> ContainerInfo:
        """
        - image: automatron/golden:latest
        - volume: {WORKSPACE_BASE_PATH}/{project_id}:/workspace
        - environment: API keys (injected at runtime)
        - port mapping: {allocated_port}:3000
        - name: automatron-{project_id}
        - user: developer
        - detach: True
        - command: sleep infinity (keep alive)
        """
    
    async def exec_in_container(
        self, container_id: str, command: str, timeout: int = 300
    ) -> ExecResult:
        """docker exec з streaming output"""
    
    async def stop_container(self, container_id: str) -> None: ...
    async def restart_container(self, container_id: str) -> None: ...
    async def get_container_logs(self, container_id: str, tail: int = 100) -> str: ...
    async def copy_file_to_container(
        self, container_id: str, content: str, container_path: str
    ) -> None: ...
    async def read_file_from_container(
        self, container_id: str, container_path: str
    ) -> str: ...
```

### 5.2. Dynamic Port Allocator

**Файл:** `orchestrator/orchestrator/docker_engine/port_allocator.py`

- Діапазон: `PORT_RANGE_START` — `PORT_RANGE_END` (default: 7000-7999)
- Зберігати зайняті порти в SQLite таблиці `port_allocations`
- `allocate_port(project_id) -> int` — знайти перший вільний порт
- `release_port(project_id)` — звільнити порт при видаленні проекту
- Перевірка: `socket.connect_ex()` для валідації що порт реально вільний

```python
class PortAllocator:
    def __init__(self, start: int = 7000, end: int = 7999):
        self.range = range(start, end + 1)
    
    async def allocate(self, project_id: str) -> int:
        """Find first free port in range, register in DB"""
    
    async def release(self, project_id: str) -> None:
        """Free allocated port"""
    
    def _is_port_free(self, port: int) -> bool:
        """Check with socket.connect_ex()"""
```

### 5.3. Scaffold Scripts

**Файл:** `orchestrator/scripts/init-*.sh`

Кожен скрипт:
1. Виконується всередині контейнера
2. `cd /workspace`
3. Ініціалізує проект відповідно до стеку
4. Встановлює залежності
5. Створює `.gitignore`

**Приклад `init-nextjs.sh`:**
```bash
#!/bin/bash
set -e
cd /workspace
npx create-next-app@latest . --typescript --tailwind --eslint --app \
    --no-src-dir --import-alias "@/*" --use-pnpm
pnpm install
echo "Scaffold complete: Next.js + TypeScript + Tailwind"
```

**Приклад `init-python.sh`:**
```bash
#!/bin/bash
set -e
cd /workspace
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
echo "Scaffold complete: Python venv"
```

**Fallback:** якщо `stack_config.init_script` не знайдено → Cline отримує першу задачу "Ініціалізуй проект з нуля"

### 5.4. Secrets Injection via Docker Secrets

- При `docker-compose.yml`: secrets визначаються як файли
- Orchestrator читає secrets з `/run/secrets/` на початку роботи
- При створенні project container — передає ключі як environment variables:
  ```python
  container = client.containers.run(
      image=golden_image,
      environment={
          "OPENAI_API_KEY": secrets["openai_api_key"],
          "ANTHROPIC_API_KEY": secrets["anthropic_api_key"],
      },
      ...
  )
  ```
- **Ключі НІКОЛИ не зберігаються в логах, SQLite, або PLAN.md**
- Scrub паттерн: перед записом будь-якого output — strip API keys

---

## Фаза 6: Web UI (Next.js)

**Estimated: 5-7 днів**

### 6.1. Dashboard Page (`/`)

- Список проектів (cards): name, status badge, progress bar, created date
- Кнопка "New Project" → модал з описом задачі
- Статуси з кольоровими badges:
  - `PLANNING` — синій
  - `SCAFFOLDING` — жовтий
  - `EXECUTING` — зелений анімований
  - `FROZEN` — червоний пульсуючий
  - `COMPLETED` — зелений ✓

### 6.2. Project Page (`/project/[id]`)

Три-панельний layout:

**Left Panel: Chat (Architect Conversation)**
- Стрімінг повідомлень через WebSocket
- Input з кнопкою Send
- Показує тільки під час фази PLANNING та ESCALATION
- Markdown rendering для повідомлень Architect

**Center Panel: PLAN.md**
- Markdown editor (`@uiw/react-md-editor`) для редагування людиною
- Preview mode (rendered markdown з чекбоксами)
- Progress counter: "12/25 tasks completed"
- Кнопка "Approve & Start Build" (disabled until plan exists)

**Right Panel: Status & Logs**
- Current phase indicator
- Current task highlight
- Real-time Cline output (terminal-style, monospace, dark bg)
- Alert banner при FROZEN/BLOCKER
- Live Preview URL link (clickable, opens in new tab)

### 6.3. WebSocket Integration

```typescript
// hooks/useWebSocket.ts
function useWebSocket(projectId: string) {
  const socket = useRef<Socket | null>(null);
  
  useEffect(() => {
    socket.current = io(WS_URL, { query: { projectId } });
    
    socket.current.on('architect:message', (data) => { /* append to chat */ });
    socket.current.on('builder:log', (data) => { /* append to logs */ });
    socket.current.on('status:update', (data) => { /* update status */ });
    socket.current.on('human:required', (data) => { /* show alert */ });
    socket.current.on('plan:updated', (data) => { /* refresh plan */ });
    
    return () => { socket.current?.disconnect(); };
  }, [projectId]);
  
  const sendMessage = (text: string) => {
    socket.current?.emit('chat:message', { text });
  };
  
  return { sendMessage };
}
```

- Auto-reconnect з exponential backoff
- Room-based: кожен project_id = окрема кімната

### 6.4. State Management (Zustand)

```typescript
interface ProjectStore {
  // State
  projects: Project[]
  currentProject: Project | null
  chatMessages: Message[]
  builderLogs: string[]
  planContent: string
  status: ProjectStatus
  progress: { completed: number; total: number }
  alerts: Alert[]
  
  // Actions
  fetchProjects: () => Promise<void>
  createProject: (name: string, description: string) => Promise<void>
  sendMessage: (text: string) => void
  approvePlan: () => Promise<void>
  stopProject: () => Promise<void>
  updatePlan: (content: string) => Promise<void>
  rollback: (checkpointId: string) => Promise<void>
  appendLog: (line: string) => void
  setAlert: (alert: Alert) => void
  clearAlert: () => void
}
```

### 6.5. Human Intervention Alert System

- При `human:required` WebSocket event:
  - Червоний banner з'являється у верхній частині Project Page
  - Показує: причину (BLOCKER reason), кількість спроб, поточну задачу
  - Кнопки: "Edit Plan & Retry", "Skip Task", "Stop Project"
  - Звуковий сигнал (browser Notification API, якщо дозволено)
  - Browser tab title мерехтить: "⚠️ Automatron — Intervention Required"

### 6.6. Time-Travel UI

- Dropdown/timeline у правій панелі
- `GET /api/projects/{id}/history` → список checkpoints з timestamp + snapshot
- Кнопка "Rollback to this point" → `POST /api/projects/{id}/rollback/{checkpoint_id}`
- Confirmation modal перед rollback
- Після rollback — Web UI оновлює plan, status, logs

---

## Фаза 7: Інтеграція та End-to-End Flow

**Estimated: 4-5 днів**

### 7.1. Full Flow Integration Test

Сценарій E2E:
1. User створює проект через Web UI → `POST /api/projects`
2. User описує задачу в чаті → WebSocket `chat:message`
3. Architect відповідає, задає уточнення → WebSocket `architect:message` (streaming)
4. User відповідає → ще питання → Architect генерує PLAN.md + STACK_CONFIG.json
5. Web UI показує PLAN.md → User натискає "Approve"
6. Orchestrator створює Docker container
7. Scaffold script ініціалізує проект
8. Builder loop: `task_selector → builder → classifier → repeat`
9. Real-time logs стрімяться в Web UI
10. При BLOCKER → escalation → Architect re-plans → retry
11. При Anti-Loop → FROZEN → Human Intervention alert
12. Після завершення всіх задач → COMPLETED

### 7.2. WebSocket Streaming Pipeline

```
LangGraph Node → AsyncQueue → FastAPI WebSocket handler → Socket.IO → Browser
```

- Кожен node emit'ить events в async queue
- WebSocket handler consume'ить queue і broadcast'ить по room (project_id)
- Architect streaming: token-by-token через litellm async streaming
- Builder streaming: line-by-line через docker exec streaming

### 7.3. Error Handling & Recovery

| Сценарій | Обробка |
|----------|---------|
| Docker container crash | Auto-restart + re-execute current task |
| LLM API timeout | Retry 3 times with exponential backoff (1s, 4s, 16s) |
| LLM API rate limit | Backoff + switch to fallback model |
| WebSocket disconnect | Auto-reconnect, replay missed events from checkpoint |
| Orchestrator crash | On restart, reload state from SQLite checkpoint |
| Cline CLI hang | Timeout kill + classify as BLOCKER |
| Disk space full | Pre-check before scaffold, alert if < 1GB |

### 7.4. Concurrent Projects

- Кожен проект = окремий LangGraph thread (`thread_id`)
- Кожен проект = окремий Docker container (ізольований)
- FastAPI async → множинні проекти одночасно
- WebSocket rooms per `project_id`
- Port allocator ensures no port conflicts
- Resource limits per container:
  ```python
  container = client.containers.run(
      ...
      mem_limit="2g",
      cpu_period=100000,
      cpu_quota=100000,  # 1 CPU core
  )
  ```

---

## Фаза 8: Hardening, Тестування, Deploy

**Estimated: 3-4 днів**

### 8.1. Unit Tests

| Test File | Coverage |
|-----------|----------|
| `test_plan_parser.py` | Парсинг PLAN.md, frontmatter, checkboxes, edge cases |
| `test_docker_engine.py` | Mock Docker SDK, container lifecycle, port allocator |
| `test_architect.py` | Mock LLM responses, plan generation, re-planning |
| `test_graph.py` | LangGraph state transitions, edge conditions, anti-loop |
| `test_status_classifier.py` | Всі 4 статуси + edge cases (empty output, timeout) |

### 8.2. Integration Tests

- Architect node + real LLM (cheap model, e.g. gpt-4.1-mini) → generates valid PLAN.md
- Builder node + real Docker + real Cline → executes simple task ("create hello.py")
- Full graph run → SQLite checkpoints created correctly
- Rollback test → state restores properly

### 8.3. Production Docker Compose

**Файл:** `docker-compose.yml`

```yaml
version: "3.8"

services:
  orchestrator:
    build:
      context: .
      dockerfile: docker/orchestrator/Dockerfile
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
      - /var/run/docker.sock:/var/run/docker.sock
      - /var/automatron/workspaces:/var/automatron/workspaces
    secrets:
      - openai_api_key
      - anthropic_api_key
      - google_api_key
    environment:
      - SQLITE_DB_PATH=/app/data/automatron.db
      - CHECKPOINT_DB_PATH=/app/data/checkpoints.db
      - WORKSPACE_BASE_PATH=/var/automatron/workspaces
      - GOLDEN_IMAGE=automatron/golden:latest
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  web-ui:
    build:
      context: .
      dockerfile: docker/web-ui/Dockerfile
    ports:
      - "3000:3000"
    environment:
      - NEXT_PUBLIC_API_URL=http://orchestrator:8000
      - NEXT_PUBLIC_WS_URL=ws://orchestrator:8000
    depends_on:
      orchestrator:
        condition: service_healthy
    restart: unless-stopped

secrets:
  openai_api_key:
    file: ./secrets/openai_api_key.txt
  anthropic_api_key:
    file: ./secrets/anthropic_api_key.txt
  google_api_key:
    file: ./secrets/google_api_key.txt
```

### 8.4. Nginx Reverse Proxy

```nginx
# /etc/nginx/sites-available/automatron
server {
    listen 80;
    server_name automatron.example.com;

    # Web UI
    location / {
        proxy_pass http://localhost:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }

    # API
    location /api/ {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # WebSocket
    location /ws/ {
        proxy_pass http://localhost:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }
}

# Project preview ports — pass-through
# Ports 7000-7999 are directly accessible
```

- Optional: Let's Encrypt для HTTPS (`certbot --nginx`)

### 8.5. Makefile

```makefile
.PHONY: dev build deploy golden test

# Development
dev:
	docker compose up --build

# Build golden image
golden:
	docker build -t automatron/golden:latest -f docker/golden-image/Dockerfile .

# Run tests
test:
	cd orchestrator && pytest tests/ -v

# Deploy to VPS
deploy:
	docker compose -f docker-compose.yml up -d --build

# View logs
logs:
	docker compose logs -f

# Clean up
clean:
	docker compose down -v
	docker system prune -f
```

---

## Верифікація

### Smoke Test (після кожної фази)

| Фаза | Перевірка |
|------|-----------|
| 1 | `docker build` golden image без помилок; `cline --version` в контейнері працює |
| 2 | `pytest tests/test_plan_parser.py` — pass; створити graph, invoke з mock nodes |
| 3 | Architect генерує валідний PLAN.md через litellm (CLI test script) |
| 4 | Builder виконує `cline -y "create hello.py with print hello"` в Docker container |
| 5 | Port allocator видає порти; контейнер стартує і доступний по mapped port |
| 6 | Web UI рендериться на `localhost:3000`, WebSocket підключається, чат працює (mock backend) |
| 7 | Full E2E: "Create a simple Python CLI calculator" → проект створюється автоматично |
| 8 | `docker compose up` на чистому VPS → система працює end-to-end |

### Acceptance Criteria (MVP Done)

- [ ] User може створити проект, поспілкуватися з Architect, отримати PLAN.md
- [ ] User може відредагувати PLAN.md і натиснути Approve
- [ ] Builder автономно виконує задачі з PLAN.md через Cline CLI в Docker
- [ ] Real-time logs стрімяться в Web UI
- [ ] BLOCKER ескалюється на Architect, який оновлює план
- [ ] Anti-Loop freeze при 3+ ескалаціях → Human Intervention alert в UI
- [ ] Time-Travel: можна переглянути checkpoints і зробити rollback
- [ ] Live Preview URL доступний для кожного проекту
- [ ] Система переживає рестарт (persistence через SQLite checkpoints)
- [ ] Модель Architect можна змінити через env var без зміни коду

---

## Прийняті Рішення

| # | Рішення | Альтернатива | Обґрунтування |
|---|---------|--------------|---------------|
| 1 | **litellm** для model-agnostic LLM | Прямі SDK (anthropic, openai) | Єдиний interface, hot-swap моделей через конфіг, підтримка 100+ providers |
| 2 | **FastAPI** замість Flask/Django | Flask, Django | Async native, Pydantic validation, WebSocket support, auto-docs |
| 3 | **Socket.IO** замість raw WebSocket | Native WebSocket | Auto-reconnect, rooms, broadcasting, fallback to polling |
| 4 | **SQLite** замість PostgreSQL | PostgreSQL, Redis | Простота для MVP, LangGraph має built-in SqliteSaver, zero-config |
| 5 | **Zustand** замість Redux | Redux, Jotai | Мінімальний boilerplate, ідеальний для mid-size app |
| 6 | **Docker Secrets** замість Vault | HashiCorp Vault, SOPS | Нативна інтеграція з Docker Compose, zero extra infra |
| 7 | **Без auth** | JWT, OAuth | Private network, single-user system, MVP scope |
| 8 | **uv** для Python packaging | Poetry, pip | Швидкість, lockfile, сумісність з pyproject.toml |
| 9 | **Cline CLI `-y` mode** | Custom agent, Aider | Mature tool, headless mode, model-agnostic, active development |
| 10 | **Notifications тільки в Web UI** | Telegram bot, Email | MVP scope, WebSocket push достатній для active user |

---

## Ризики та Мітигація

| Ризик | Імовірність | Вплив | Мітигація |
|-------|-------------|-------|-----------|
| Cline CLI headless mode нестабільний для складних задач | Середня | Високий | Timeout + retry; fallback на простіший prompt; розбивати задачі дрібніше в PLAN.md |
| LLM галюцинації в PLAN.md | Середня | Середній | Context blocks в плані; global_rules як guardrails; Human review перед Approve |
| Docker socket access = security risk | Низька | Високий | Private network; непривілейований user в контейнерах; ізоляція volumes per project |
| SQLite не витримає concurrent writes | Низька | Середній | WAL mode; одночасно тільки 1 builder per project; для масштабу → міграція на PostgreSQL |
| Port exhaustion при багатьох проектах | Низька | Низький | 1000 портів (7000-7999); cleanup при видаленні; моніторинг зайнятих портів |
| LangGraph breaking changes | Низька | Середній | Зафіксувати версію в pyproject.toml; тести покривають graph behavior |
| Cline output parsing fails | Середня | Середній | Fallback: якщо LLM classifier не може визначити статус → AMBIGUITY → ескалація |
| Container disk space overflow | Низька | Середній | `--storage-opt size=10G` per container; моніторинг; cleanup old containers |

---

## Часова Оцінка (Summary)

| Фаза | Тривалість | Залежності | Паралелізація |
|------|------------|------------|---------------|
| Фаза 1: Scaffolding | 2-3 дні | — | — |
| Фаза 2: Orchestrator Core | 5-7 днів | Фаза 1 | — |
| Фаза 3: Architect Module | 3-4 дні | Фаза 2 | ⇄ з Фазою 5, 6 |
| Фаза 4: Builder Module | 4-5 днів | Фаза 2, Фаза 5 | — |
| Фаза 5: Docker Engine | 3-4 дні | Фаза 1 | ⇄ з Фазою 3, 6 |
| Фаза 6: Web UI | 5-7 днів | Фаза 2 (API) | ⇄ з Фазою 3, 5 |
| Фаза 7: Integration | 4-5 днів | Фази 3-6 | — |
| Фаза 8: Hardening & Deploy | 3-4 днів | Фаза 7 | — |
| **TOTAL (послідовно)** | **29-39 днів** | — | — |
| **TOTAL (з паралелізацією)** | **~20-25 днів** | — | Фази 3, 5, 6 паралельно |

### Діаграма залежностей фаз

```
Фаза 1 ──→ Фаза 2 ──┬──→ Фаза 3 ──┐
    │                 │              │
    └──→ Фаза 5 ──┐  └──→ Фаза 6 ──┤
                   │                │
                   └──→ Фаза 4 ─────┤
                                    │
                                    └──→ Фаза 7 ──→ Фаза 8
```
