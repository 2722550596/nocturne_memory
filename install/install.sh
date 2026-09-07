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

# Extensions are now machine-independent: all local paths come from
# nocturne-memory.config.json (created below), so the same files work on any
# machine. No more install-time {{PLACEHOLDER}} substitution in the .ts files.
for ext in nocturne-memory.ts nocturne-memory-recall.ts nocturne-memory-tools.ts; do
    cp "${INSTALL_DIR}/extensions/${ext}" "${EXT_DIR}/${ext}"
    echo "    Installed/Updated: ${ext}"
done

# Machine-specific config: created once, never overwritten afterwards, so each
# machine keeps its own paths (project dir, agent dir, API base, tokens). To
# move to another machine, copy the extensions + edit this one JSON file.
EXT_CONFIG="${EXT_DIR}/nocturne-memory.config.json"
if [ ! -f "$EXT_CONFIG" ]; then
    MEMORY_API="${NOCTURNE_MEMORY_API:-http://127.0.0.1:8233}"
    sed -e "s|{{MEMORY_DIR}}|${PROJECT_DIR}|g" \
        -e "s|{{PI_AGENT_DIR}}|${PI_AGENT}|g" \
        -e "s|{{MEMORY_API}}|${MEMORY_API}|g" \
        -e "s|{{API_TOKEN}}|${NOCTURNE_API_TOKEN:-}|g" \
        -e "s|{{EMBEDDING_API_KEY}}|${NOCTURNE_EMBEDDING_API_KEY:-}|g" \
        "${INSTALL_DIR}/extensions/nocturne-memory.config.json" > "$EXT_CONFIG"
    echo "    Created: ${EXT_CONFIG}"
else
    echo "    Keep: ${EXT_CONFIG} (exists — edit it to change paths)"
fi

# Install parse-think-tags.ts (symlink)
if [ ! -f "${EXT_DIR}/parse-think-tags.ts" ]; then
    ln -s "${INSTALL_DIR}/extensions/parse-think-tags.ts" "${EXT_DIR}/parse-think-tags.ts"
    echo "    Linked: parse-think-tags.ts"
else
    echo "    Skip: parse-think-tags.ts (already exists)"
fi

# 2b. Install systemd user service for the backend (the tools extension
# requires the web server running). Created once so local edits survive;
# daemon-reload + enable so it starts on boot even without a login.
mkdir -p "${HOME}/.config/systemd/user"
UNIT="${HOME}/.config/systemd/user/nocturne-memory.service"
if [ ! -f "$UNIT" ]; then
    sed -e "s|{{MEMORY_DIR}}|${PROJECT_DIR}|g" \
        "${INSTALL_DIR}/systemd/nocturne-memory.service" > "$UNIT"
    echo "    Installed: ${UNIT}"
    systemctl --user daemon-reload 2>/dev/null && \
        systemctl --user enable --now nocturne-memory 2>/dev/null || \
        echo "    (systemd unavailable — start manually: venv/bin/python ./backend/run_sse.py)"
else
    echo "    Keep: ${UNIT} (exists — edit to change backend launch)"
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
