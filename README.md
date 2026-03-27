# Autonomous Agent Platform

An always-on autonomous software engineering agent that executes long-running tasks overnight without human supervision. Submit a goal in plain English — the agent plans, codes, tests, commits, and reports back.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Browser  /ui                             │
│          Task List · Live Step Progress · Audit Trail           │
│          Submit · Cancel · Backtrack                            │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTP
┌──────────────────────────▼──────────────────────────────────────┐
│                     FastAPI  :8000                              │
│  POST /v1/tasks   GET /v1/tasks/:id   POST /v1/tasks/:id/       │
│  backtrack/:step  POST /v1/tasks/:id/cancel   GET /v1/jobs      │
│  GET /v1/tasks/:id/audit   GET /v1/tasks/:id/report             │
└──────┬──────────────────────────────────────────────────────────┘
       │ Background Task (asyncio)
┌──────▼──────────────────────────────────────────────────────────┐
│                      Orchestrator                               │
│                                                                 │
│  1. Planner  ──► Claude API  ──► Structured step plan           │
│  2. Policy   ──► Claude API  ──► Risk level + budget rules      │
│  3. Persist  ──► PostgreSQL  ──► Task + Step records            │
│  4. Execute  ──► Executor (per step, see below)                 │
│  5. Reporter ──► Claude API  ──► Final summary → S3             │
│                                                                 │
│  Backtrack Engine: reset steps N..end → pending, re-run         │
└──────┬──────────────────────────────────────────────────────────┘
       │ Per step (asyncio.to_thread)
┌──────▼──────────────────────────────────────────────────────────┐
│                    Executor Agent                               │
│                                                                 │
│  Claude tool-use loop (max 30 iterations / step)               │
│                                                                 │
│  Tools available to Claude:                                     │
│   repo_list_files   repo_read_file    repo_write_file           │
│   repo_apply_patch  repo_create_branch  repo_commit             │
│   repo_get_diff     shell_exec         tests_run                │
│   request_permission  step_complete                             │
│                                                                 │
│  Git Workspace (temp dir per task)                              │
│   – init / clone repo                                           │
│   – real git commits with SHA per step                          │
│   – teardown after task completes                               │
└──────┬──────────────────────────────────────────────────────────┘
       │
┌──────▼──────────────────────────────────────────────────────────┐
│                       Storage Layer                             │
│                                                                 │
│  PostgreSQL  ── canonical state (tasks, steps, approvals,       │
│                  test_runs)                                     │
│  DynamoDB    ── append-only event journal + checkpoints         │
│  S3          ── artifacts (reports, screenshots, patches)       │
└─────────────────────────────────────────────────────────────────┘
```

---

## Full Request Flow

```
User submits goal via UI / API
        │
        ▼
POST /v1/tasks
  → idempotency check (prevents duplicate runs)
  → create Task record (PostgreSQL, status=pending)
  → emit task.submitted event (DynamoDB)
  → spawn background orchestration task
        │
        ▼
Orchestrator.run()
  ├─ Planner  → Claude API → JSON step plan (risk_level, steps[])
  ├─ Policy   → Claude API → boundary rules (max_cost, permissions)
  ├─ Persist  → PostgreSQL (task status=running, step records)
  │
  └─ For each step:
        ├─ update step status=running + current_step_id
        ├─ write DynamoDB checkpoint (resume point)
        │
        └─ Executor.run_step()
              ├─ Build user message (step action + workspace overview)
              ├─ Claude tool-use loop:
              │     invoke Claude → tool_use response
              │     → dispatch tool → result back to Claude
              │     → repeat until step_complete or max_iterations
              ├─ Persist step outcome (status, base_sha, head_sha)
              └─ Persist test results if tests_run was called
        │
        ▼
Reporter → Claude API → summary JSON → S3
Task status = completed / failed
        │
        ▼
UI auto-refreshes every 4s → steps turn green one by one
```

---

## Backtrack Flow

```
User clicks Backtrack on step N
        │
        ▼
POST /v1/tasks/:id/backtrack/:step_id  { reason }
        │
        ▼
backtrack.execute_backtrack()
  ├─ Validate task + step exist
  ├─ Reset steps[N..end] → status=pending, clear SHAs
  ├─ Generate new run_id
  ├─ Reset task → status=running
  ├─ Emit backtrack.executed event (DynamoDB)
  └─ Spawn new orchestrator run (start_from_index=N)
        │
        ▼
Orchestrator reuses stored plan (no re-planning)
Skips steps 0..N-1 (already completed, untouched)
Re-executes steps N..end in a fresh git workspace
New git SHAs produced for each re-run step
        │
        ▼
UI shows steps N..end go pending → running → completed
```

---

## Project Structure

```
autonomous-agent-platform/
├── docker-compose.yml              # One-command local stack
├── .env.example                    # All env vars documented
├── Makefile                        # dev/test/fmt shortcuts
│
├── backend/
│   ├── Dockerfile
│   ├── pyproject.toml
│   └── src/
│       ├── api/
│       │   ├── main.py             # FastAPI app + /ui route
│       │   ├── schemas.py          # Pydantic I/O contracts
│       │   ├── static/index.html   # Demo UI (vanilla JS, no build)
│       │   └── routes/
│       │       ├── tasks.py        # submit, status, backtrack, cancel
│       │       ├── jobs.py         # task list for UI
│       │       ├── audit.py        # DynamoDB event trail
│       │       └── approvals.py    # human approval decisions
│       │
│       ├── agents/
│       │   ├── orchestrator/
│       │   │   ├── agent.py        # Main loop (plan→execute→report)
│       │   │   ├── planner.py      # Claude → structured step plan
│       │   │   ├── policy.py       # Claude → boundary + budget rules
│       │   │   ├── reporter.py     # Claude → final summary → S3
│       │   │   ├── permission.py   # auto-approve / LLM evaluate
│       │   │   └── backtrack.py    # rewind + re-run engine
│       │   └── executor/
│       │       ├── runner.py       # Claude tool-use loop per step
│       │       ├── tools.py        # 11 typed tool implementations
│       │       └── workspace.py    # git workspace lifecycle
│       │
│       ├── llm/
│       │   ├── client.py           # Anthropic API wrapper
│       │   └── prompts/            # System prompts (markdown)
│       │
│       ├── storage/
│       │   ├── rds.py              # PostgreSQL (SQLAlchemy)
│       │   ├── dynamo.py           # DynamoDB event journal
│       │   └── s3.py               # S3 artifact store
│       │
│       └── common/
│           ├── config.py           # pydantic-settings env loader
│           ├── logging.py          # structlog setup
│           └── utils.py            # ID generators, hashing
│
└── infra/
    └── localstack-init/init.sh     # Auto-creates S3/SQS/DynamoDB
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.11 |
| API | FastAPI + Uvicorn |
| AI | Anthropic Claude (claude-sonnet-4-6) via raw Messages API |
| Agent loop | Claude tool_use with 11 typed tools |
| Git ops | subprocess git in isolated temp workspace |
| State DB | PostgreSQL 16 (SQLAlchemy) |
| Event log | DynamoDB (append-only journal) |
| Artifacts | S3 (reports, patches) |
| Local AWS | LocalStack 3 |
| UI | Vanilla JS + Bootstrap 5 (no build step) |
| Containers | Docker Compose |

---

## Current State

### Working end-to-end
- Submit any plain-English goal → Claude decomposes into 5–10 steps with risk assessment
- Executor runs each step: reads files, writes code, creates branches, runs tests, commits — all real git operations with unique SHAs per step
- Backtrack to any prior step — resets downstream steps and re-executes from that point with new commits
- Cancel running task
- Full audit trail (every event in DynamoDB with timestamps)
- Demo UI at `/ui` with live step progress (auto-refresh every 4s), audit trail viewer, report link
- One-command local start: `docker compose up`

### Not yet implemented
- ECS Fargate container isolation per step (currently runs in the API process)
- Real GitHub repo cloning with auth (demo workspace is a local temp git repo)
- Step Functions for task queue (currently uses FastAPI background tasks)
- MCP / Confluence integration
- WebSocket live push (currently polls every 4s)
- Multi-tenant isolation
- Cost tracking per task (token counts tracked but not billed)

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/MohitTripathi259/Auto-Task-Execution.git
cd Auto-Task-Execution

# 2. Configure
cp .env.example .env
# Edit .env — set ANTHROPIC_API_KEY

# 3. Start
docker compose up --build

# 4. Open UI
open http://localhost:8000/ui

# 5. Submit a task (or use the UI)
curl -X POST http://localhost:8000/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"goal": "Add email validation to the registration form", "tenant_id": "default"}'
```

---

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key | required |
| `APP_ENV` | `local` \| `dev` \| `prod` | `local` |
| `AWS_REGION` | AWS region | `us-west-2` |
| `AWS_ENDPOINT_URL` | LocalStack URL in local mode | `http://localstack:4566` |
| `S3_BUCKET` | Artifact bucket name | `agent-artifacts` |
| `DYNAMODB_TABLE_EVENTS` | Event journal table | `agent_events` |
| `DATABASE_URL` | PostgreSQL connection string | set in compose |
| `CLAUDE_PLANNER_MODEL` | Model for planning/policy/report | `claude-sonnet-4-6` |
| `CLAUDE_EXECUTOR_MODEL` | Model for step execution | `claude-sonnet-4-6` |

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/tasks` | Submit a new task |
| `GET` | `/v1/tasks/:id` | Get task status + steps |
| `POST` | `/v1/tasks/:id/cancel` | Cancel a running task |
| `POST` | `/v1/tasks/:id/backtrack/:step_id` | Rewind to a prior step |
| `GET` | `/v1/tasks/:id/audit` | Full DynamoDB event trail |
| `GET` | `/v1/tasks/:id/report` | Pre-signed S3 report URL |
| `GET` | `/v1/jobs` | List all tasks (UI polling) |
| `GET` | `/ui` | Demo web interface |
| `GET` | `/health` | Health check |
