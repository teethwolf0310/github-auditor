#!/bin/bash
# 端到端 smoke：先跑 pytest，再（如果有 token）真实评估一个 handle。
set -e
cd "$(dirname "$0")/.."

PYTHON=/home/zhoupeng/dev/github-auditor-venv/bin/python

echo "=== step 1: offline unit tests ==="
$PYTHON -m pytest tests/ -q

echo ""
echo "=== step 2: cli help ==="
$PYTHON -m app.main --help | head -20

if [[ ! -f config.local.yaml ]]; then
    echo ""
    echo "  (skip online eval: 没有 config.local.yaml，请先 cp config.example.yaml config.local.yaml 并填 token)"
    exit 0
fi

HANDLE=${1:-torvalds}
echo ""
echo "=== step 3: live eval ($HANDLE) ==="
$PYTHON -m app.main eval "$HANDLE" --report

echo ""
echo "=== step 4: list ==="
$PYTHON -m app.main list
