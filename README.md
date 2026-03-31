# Autonomous Agent Platform

An always-on autonomous software engineering agent that executes long-running tasks without human supervision. Submit a goal in plain English — the agent plans, codes, tests, commits, and reports back.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Browser  /ui                             │
│   Task List · Live Step Progress · Audit Trail · Backtrack      │
│   New Task modal with Task Skill upload                         │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTP
┌──────────────────────────▼──────────────────────────────────────┐
│                     FastAPI  :8000                              │
│  POST /v1/tasks        GET /v1/tasks/:id                        │
│  POST /v1/tasks/:id/backtrack/:step                             │
│  POST /v1/tasks/:id/cancel    GET /v1/jobs                      │
│  GET /v1/tasks/:id/audit      GET /v1/tasks/:id/report          │
└──────┬──────────────────────────────────────────────────────────┘
       │ Background Task (asyncio)
┌──────▼──────────────────────────────────────────────────────────┐
│                      Orchestrator                               │
│                                                                 │
│  1. Planner skill  → structured step plan                       │
│  2. Policy skill   → risk level + budget rules                  │
│  3. Persist        → PostgreSQL (task + step records)           │
│  4. Executor skill → Claude tool-use loop (per step)            │
│  5. GitHub PR skill → auto-open PR (if token configured)        │
│  6. Reporter skill → final summary → S3                         │
│                                                                 │
│  Backtrack Engine: reset steps N..end → pending, re-run         │
└──────┬──────────────────────────────────────────────────────────┘
       │ Per step
┌──────▼──────────────────────────────────────────────────────────┐
│                    Skill-Based Architecture                     │
│                                                                 │
│  Each skill = skill.md (spec/rules) + execution.py (code)      │
│                                                                 │
│  src/skills/                                                    │
│    planner/    skill.md + execution.py                          │
│    policy/     skill.md + execution.py                          │
│    executor/   skill.md + execution.py                          │
│    reporter/   skill.md + execution.py                          │
│    github_pr/  skill.md + execution.py                          │
│                                                                 │
│  Task-specific skill: user pastes markdown at submission        │
│  → merged into every step's system prompt via skill loader      │
└──────┬──────────────────────────────────────────────────────────┘
       │
┌──────▼──────────────────────────────────────────────────────────┐
│                    Executor Agent (per step)                    │
│                                                                 │
│  Claude tool-use loop (max 30 iterations / step)               │
│                                                                 │
│  Tools available:                                               │
│   repo_list_files   repo_read_file    repo_write_file           │
│   repo_apply_patch  repo_create_branch  repo_commit             │
│   repo_get_diff     shell_exec         tests_run                │
│   request_permission  step_complete                             │
│                                                                 │
│  Git Workspace (isolated temp dir per task)                     │
│   – init empty repo or clone from GitHub                        │
│   – real git commits with SHA per step                          │
│   – feature branch captured for PR creation                     │
│   – teardown after task completes                               │
└──────┬──────────────────────────────────────────────────────────┘
       │
┌──────▼──────────────────────────────────────────────────────────┐
│                       Storage Layer                             │
│                                                                 │
│  PostgreSQL  ── tasks, steps, approvals, test_runs              │
│                  + task_skill_content, pr_url columns           │
│  DynamoDB    ── append-only event journal + checkpoints         │
│  S3          ── artifacts (reports, patches)                    │
└─────────────────────────────────────────────────────────────────┘
```

---

## Skill-Based Architecture

Each capability is a self-contained skill folder:

```
src/skills/
  planner/
    skill.md       ← role, output schema, rules, limitations
    execution.py   ← calls Claude API, returns structured plan
  policy/
    skill.md
    execution.py
  executor/
    skill.md
    execution.py
  reporter/
    skill.md
    execution.py
  github_pr/
    skill.md
    execution.py   ← opens PR via GitHub API (optional)
  loader.py        ← build_system_prompt(skill, task_skill_content)
```

**Task-specific skills**: at task submission, users can paste a custom `skill.md` in the UI. The skill loader merges it with the base skill spec and injects it into every step's system prompt.

---

## Key Features

- **Skill architecture** — each agent capability has its own spec (`skill.md`) and code (`execution.py`); no hardcoded prompts
- **Task skill upload** — domain-specific instructions injected at runtime per task
- **Real git commits** — each step produces an actual commit with a unique SHA
- **Backtrack engine** — reset any step and re-run from that point forward
- **GitHub PR (optional)** — auto-opens a PR after task completes if `GITHUB_TOKEN` is set
- **Audit trail** — every event logged to DynamoDB; viewable in the UI
- **Cancel** — stop any running task mid-execution

---

## Quick Start

### Prerequisites
- Docker + Docker Compose
- Anthropic API key

### 1. Configure environment

```bash
cp .env.example .env
# Edit .env — set ANTHROPIC_API_KEY
# Optionally set GITHUB_TOKEN for PR creation
```

### 2. Start the stack

```bash
docker compose up --build
```

Starts: FastAPI (port 8000) + PostgreSQL + LocalStack (S3, DynamoDB)

### 3. Open the UI

```
http://localhost:8000/ui
```

### 4. Submit a task

Click **New Task** and fill in:

| Field | Example |
|-------|---------|
| Goal | `Create a Python BankAccount class with deposit, withdraw, unit tests` |
| Repository URL | leave blank (demo workspace) |
| Max Cost | `3.00` |
| Task Skill | paste custom `skill.md` content (optional) |

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/tasks` | Submit a new task |
| `GET` | `/v1/tasks/:id` | Get task status + steps |
| `GET` | `/v1/tasks/:id/audit` | Full event audit trail |
| `GET` | `/v1/tasks/:id/report` | Final report (S3 URL) |
| `POST` | `/v1/tasks/:id/backtrack/:step_id` | Re-run from a step |
| `POST` | `/v1/tasks/:id/cancel` | Cancel running task |
| `GET` | `/v1/jobs` | List all tasks |

### Submit Task Request

```json
{
  "goal": "Add phone validation to checkout form",
  "repo_url": "https://github.com/your-org/your-repo",
  "base_branch": "main",
  "max_cost_usd": 5.0,
  "max_runtime_minutes": 60,
  "tenant_id": "default",
  "task_skill": "## Role\nYou are a Python TDD specialist...(optional)"
}
```

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Required — Anthropic API key |
| `GITHUB_TOKEN` | Optional — enables auto PR creation |
| `CLAUDE_PLANNER_MODEL` | Model for planner/policy/reporter (default: `claude-sonnet-4-6`) |
| `CLAUDE_EXECUTOR_MODEL` | Model for executor steps (default: `claude-sonnet-4-6`) |
| `S3_BUCKET` | S3 bucket for artifacts |
| `DYNAMODB_TABLE_EVENTS` | DynamoDB table for event journal |
| `DATABASE_URL` | PostgreSQL connection string |

---

## Project Structure

```
autonomous-agent-platform/
  backend/
    src/
      api/
        main.py          FastAPI app + routes
        routes/          tasks, jobs, audit, approvals
        schemas.py       Pydantic request/response models
        static/
          index.html     Demo UI (vanilla JS + Bootstrap 5)
      agents/
        orchestrator/
          agent.py       Main orchestration loop
          backtrack.py   Backtrack engine
          permission.py  Permission request handler
        executor/
          tools.py       11 Claude tools (file, git, shell, tests)
          workspace.py   Git workspace lifecycle
      skills/
        planner/         skill.md + execution.py
        policy/          skill.md + execution.py
        executor/        skill.md + execution.py
        reporter/        skill.md + execution.py
        github_pr/       skill.md + execution.py
        loader.py        Skill loader + prompt builder
      llm/
        client.py        Anthropic API helpers
      storage/
        rds.py           PostgreSQL models + CRUD
        dynamo.py        DynamoDB event journal
        s3.py            S3 artifact store
      common/
        config.py        Pydantic settings
        logging.py       Structlog setup
    dump_files/          Old architecture files (superseded by skills)
  docker-compose.yml
  .env.example
```
