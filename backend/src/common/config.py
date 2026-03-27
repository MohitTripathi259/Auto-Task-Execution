from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    APP_ENV: str = "local"
    LOG_LEVEL: str = "INFO"

    # Anthropic — control plane uses raw API
    ANTHROPIC_API_KEY: str = ""
    CLAUDE_PLANNER_MODEL: str = "claude-sonnet-4-6"
    CLAUDE_EXECUTOR_MODEL: str = "claude-sonnet-4-6"

    # AWS
    AWS_REGION: str = "us-west-2"
    AWS_ACCESS_KEY_ID: str = "test"
    AWS_SECRET_ACCESS_KEY: str = "test"
    AWS_ENDPOINT_URL: str | None = None  # set to LocalStack URL in local mode

    # Storage
    S3_BUCKET: str = "agent-artifacts"
    DYNAMODB_TABLE_EVENTS: str = "agent_events"

    # Database (PostgreSQL — canonical control-plane state)
    DATABASE_URL: str = "postgresql://agent:agent@localhost:5432/agentdb"

    # SQS (task queue — Step Functions replaces this post-demo)
    SQS_TASK_QUEUE_URL: str = ""
    SQS_VISIBILITY_TIMEOUT: int = 300  # seconds

    # ECS (prod only; local mode uses Docker SDK)
    ECS_CLUSTER: str = "agent-cluster"
    ECS_TASK_DEFINITION: str = "agent-executor"
    ECS_SUBNET_IDS: list[str] = []
    ECS_SECURITY_GROUP_IDS: list[str] = []

    # GitHub
    GITHUB_TOKEN: str = ""

    # Notifications
    SES_FROM_EMAIL: str = "agent@example.com"
    ALERT_EMAIL: str = ""

    # Budget defaults (can be overridden per task)
    DEFAULT_MAX_RUNTIME_MINUTES: int = 60
    DEFAULT_MAX_TOOL_CALLS: int = 40
    DEFAULT_MAX_COST_USD: float = 8.0
    DEFAULT_MAX_INPUT_TOKENS: int = 120_000
    DEFAULT_MAX_OUTPUT_TOKENS: int = 25_000


settings = Settings()
