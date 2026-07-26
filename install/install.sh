#!/usr/bin/env bash
set -euo pipefail

# Get the absolute path of the project root (one level up from this script)
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR="${PROJECT_DIR}/install"

PI_AGENT="${HOME}/.pi/agent"
EXT_DIR="${PI_AGENT}/extensions"
PRE_DIR="${PI_AGENT}/prompt-presets"

echo "==> Nocturne Memory pi Integration Installer"

# 1. Create pi directories
mkdir -p "$EXT_DIR" "$PRE_DIR"

# 2. Process and Install Extensions
echo "==> Installing Extensions..."

# Process nocturne-memory.ts to inject the actual path
TMP_EXT=$(mktemp)
sed "s|{{MEMORY_DIR}}|${PROJECT_DIR}|g" "${INSTALL_DIR}/extensions/nocturne-memory.ts" > "$TMP_EXT"

if [ ! -f "${EXT_DIR}/nocturne-memory.ts" ]; then
    cp "$TMP_EXT" "${EXT_DIR}/nocturne-memory.ts"
    echo "    Installed: nocturne-memory.ts"
else
    echo "    Updating: nocturne-memory.ts"
    cp "$TMP_EXT" "${EXT_DIR}/nocturne-memory.ts"
fi
rm "$TMP_EXT"

# Install parse-think-tags.ts (symlink)
if [ ! -f "${EXT_DIR}/parse-think-tags.ts" ]; then
    ln -s "${INSTALL_DIR}/extensions/parse-think-tags.ts" "${EXT_DIR}/parse-think-tags.ts"
    echo "    Linked: parse-think-tags.ts"
else
    echo "    Skip: parse-think-tags.ts (already exists)"
fi

# 3. Install Prompt Presets
echo "==> Installing Prompt Presets..."
for f in "${INSTALL_DIR}/prompt-presets"/*.json; do
    name=$(basename "$f")
    if [ ! -f "${PRE_DIR}/$name" ]; then
        ln -s "$f" "${PRE_DIR}/$name"
        echo "    Linked: $name"
    else
        echo "    Skip: $name (already exists)"
    fi
done

# 4. Generate mcp.json suggestion
echo "==> Generating mcp.json configuration..."
MCP_CONFIG="${PI_AGENT}/mcp.json"
PYTHON_BIN="${PROJECT_DIR}/venv/bin/python"
SERVER_SCRIPT="${PROJECT_DIR}/backend/mcp_server.py"
PYTHONPATH="${PROJECT_DIR}/backend"

# If mcp.json doesn't exist, create it. If it does, show what to add.
if [ ! -f "$MCP_CONFIG" ]; then
    cat > "$MCP_CONFIG" <<EOF
{
  "settings": {
    "directTools": true,
    "disableProxyTool": true
  },
  "mcpServers": {
    "": {
      "command": "${PYTHON_BIN}",
      "args": ["${SERVER_SCRIPT}"],
      "env": { "NAMESPACE": "klein", "PYTHONPATH": "${PYTHONPATH}" }
    }
  }
}
EOF
    echo "    Created: ${MCP_CONFIG} (Default namespace: klein)"
else
    echo "    !!! Warning: ${MCP_CONFIG} already exists."
    echo "    Please manually add the MCP server to your mcpServers."
    echo "    To get tools without prefix (e.g. 'search_memory'), use \"\" as the key:"
    echo ""
    echo "    \"\": {"
    echo "      \"command\": \"${PYTHON_BIN}\","
    echo "      \"args\": [\"${SERVER_SCRIPT}\"],"
    echo "      \"env\": { \"NAMESPACE\": \"klein\", \"PYTHONPATH\": \"${PYTHONPATH}\" }"
    echo "    }"
    echo ""
fi

echo "==> Done!"
echo "    Presets installed: klein-chat, elias-chat"
echo "    To start: pi --preset klein-chat"
