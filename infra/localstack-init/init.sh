#!/bin/bash
# LocalStack auto-init — runs once when LocalStack is ready
set -e

echo "==> Creating S3 bucket..."
awslocal s3 mb s3://agent-artifacts || true

echo "==> Creating SQS FIFO queue..."
awslocal sqs create-queue \
  --queue-name agent-tasks.fifo \
  --attributes FifoQueue=true,ContentBasedDeduplication=true || true

echo "==> Creating DynamoDB table..."
awslocal dynamodb create-table \
  --table-name agent_events \
  --attribute-definitions \
    AttributeName=pk,AttributeType=S \
    AttributeName=sk,AttributeType=S \
  --key-schema \
    AttributeName=pk,KeyType=HASH \
    AttributeName=sk,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST || true

echo "==> LocalStack resources ready."
