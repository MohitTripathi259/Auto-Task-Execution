.PHONY: dev dev-backend test lint fmt setup setup-infra logs

# ── Local dev ─────────────────────────────────────────────────────────────────
dev:
	docker compose up --build

dev-backend:
	cd backend && uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000

logs:
	docker compose logs -f api

# ── Testing ───────────────────────────────────────────────────────────────────
test:
	cd backend && pytest tests/ -v --cov=src --cov-report=term-missing

test-unit:
	cd backend && pytest tests/unit/ -v

# ── Code quality ──────────────────────────────────────────────────────────────
lint:
	cd backend && ruff check src/ tests/

fmt:
	cd backend && ruff format src/ tests/

# ── Setup ─────────────────────────────────────────────────────────────────────
setup:
	cd backend && pip install -e ".[dev]"

# Creates LocalStack resources (run after `make dev` starts)
setup-infra:
	@echo "Creating S3 bucket..."
	aws --endpoint-url=http://localhost:4566 s3 mb s3://agent-artifacts --region us-east-1 || true
	@echo "Creating SQS FIFO queue..."
	aws --endpoint-url=http://localhost:4566 sqs create-queue \
		--queue-name agent-tasks.fifo \
		--attributes FifoQueue=true,ContentBasedDeduplication=true \
		--region us-east-1 || true
	@echo "Creating DynamoDB table..."
	aws --endpoint-url=http://localhost:4566 dynamodb create-table \
		--table-name agent_events \
		--attribute-definitions \
			AttributeName=pk,AttributeType=S \
			AttributeName=sk,AttributeType=S \
		--key-schema \
			AttributeName=pk,KeyType=HASH \
			AttributeName=sk,KeyType=RANGE \
		--billing-mode PAY_PER_REQUEST \
		--region us-east-1 || true
	@echo "Infra ready."
