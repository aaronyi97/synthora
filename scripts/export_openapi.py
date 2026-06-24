#!/usr/bin/env python3
"""
Export OpenAPI schema from FastAPI app to JSON file.

Usage:
    python3 scripts/export_openapi.py > web/openapi.json

This is Step 1 of the contract auto-generation pipeline (禁令 #9).
Step 2: npx openapi-typescript web/openapi.json -o web/src/types/generated.ts
"""

import json
import sys
from pathlib import Path

# Add project src to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agoracle.api.app import create_app


def _normalize_binary_schema(node):
    if isinstance(node, dict):
        if (
            node.get("type") == "string"
            and node.get("format") == "binary"
            and "contentMediaType" not in node
        ):
            original_items = list(node.items())
            node.clear()
            for key, value in original_items:
                if key == "format":
                    node["contentMediaType"] = "application/octet-stream"
                else:
                    node[key] = value

        for value in node.values():
            _normalize_binary_schema(value)
    elif isinstance(node, list):
        for item in node:
            _normalize_binary_schema(item)


app = create_app()
schema = app.openapi()
_normalize_binary_schema(schema)

# Write to stdout (pipe to file) or to default location
if len(sys.argv) > 1:
    output_path = Path(sys.argv[1])
    output_path.write_text(json.dumps(schema, indent=2, ensure_ascii=False))
    print(f"OpenAPI schema written to {output_path}", file=sys.stderr)
else:
    print(json.dumps(schema, indent=2, ensure_ascii=False))
