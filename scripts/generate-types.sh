#!/bin/bash
# Contract auto-generation pipeline (禁令 #9: 禁止手写双份契约)
#
# Generates TypeScript types from FastAPI Pydantic models:
#   1. Export OpenAPI schema from FastAPI app → web/openapi.json
#   2. Generate TS types from schema → web/src/types/generated.ts
#
# Usage:
#   ./scripts/generate-types.sh
#   # or from web/: npm run generate-types

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
WEB_DIR="$PROJECT_ROOT/web"
SCHEMA_FILE="$WEB_DIR/openapi.json"
OUTPUT_FILE="$WEB_DIR/src/types/generated.ts"

echo "=== Step 1: Export OpenAPI schema ==="
cd "$PROJECT_ROOT"
python3 scripts/export_openapi.py "$SCHEMA_FILE"

echo "=== Step 2: Generate TypeScript types ==="
cd "$WEB_DIR"
npx openapi-typescript "$SCHEMA_FILE" -o "$OUTPUT_FILE"

echo "=== Done: $OUTPUT_FILE ==="
echo "Remember to check git diff before committing."
