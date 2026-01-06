#!/bin/bash
set -e

echo "========================================="
echo "Syncing Cekura API Whitelist"
echo "========================================="
echo ""

if [ ! -f "../mint.json" ]; then
    echo "❌ Error: ../mint.json not found!"
    echo "   Please ensure mint.json is in the docs root directory"
    exit 1
fi

if [ ! -f "../openapi.json" ]; then
    echo "⚠️  Warning: ../openapi.json not found!"
    echo "   The extraction will fail without the OpenAPI spec"
fi

echo "🔄 Regenerating documented_apis.json from mint.json..."
python3 extract_documented_apis.py

if [ $? -eq 0 ]; then
    echo ""
    echo "========================================="
    echo "✅ API whitelist updated successfully!"
    echo "========================================="
    echo ""
    echo "Next steps:"
    echo "  1. Review documented_apis.json"
    echo "  2. Restart the MCP server to apply changes"
    echo ""
else
    echo ""
    echo "❌ Failed to update API whitelist"
    exit 1
fi
