# Nocturne Memory pi Integration

This folder contains the "glue layer" to integrate Nocturne Memory with [pi](https://github.com/earendil-works/pi).

## Features

- **Memory Slots**: Registers `nocturne-memory-boot`, `nocturne-memory-history`, and `nocturne-memory-state` slots for use in prompt presets.
- **MCP Tools**: Integrates memory tools (`search_memory`, `remember_memory`, etc.) via the Model Context Protocol.
- **Thinking Tag Parsing**: Robustly handles `<think>` or `<thinking>` blocks, converting them into native pi thinking components in the TUI.
- **Independent Characters**: Includes pre-configured presets for characters like Klein Moretti and Elias Thorne.

## Requirements

1.  **pi**: `npm install -g @earendil-works/pi`
2.  **pi-mcp-adapter**: `pi install npm:pi-mcp-adapter`
3.  **Python Venv**: Ensure you have set up the virtual environment in the project root:
    ```bash
    python3 -m venv venv
    ./venv/bin/pip install -r requirements.txt
    ```

## Installation

Run the install script from this directory:

```bash
bash install.sh
```

This will:
- Link/Copy extensions to `~/.pi/agent/extensions/`.
- Link prompt presets to `~/.pi/agent/prompt-presets/`.
- Configure `~/.pi/agent/mcp.json` to point to the local Nocturne Memory MCP server.

## Usage

Start pi with one of the character presets:

```bash
pi --preset klein-chat
# or
pi --preset elias-chat
```

## Character Configuration

The presets reference specific "namespaces" in the memory system. For example, `klein-chat.json` uses `options: { "namespace": "klein" }`.

To create a new character:
1.  Add your data to the SQLite database (defaults to `demo.db` in the project root).
2.  Create a new prompt preset in `~/.pi/agent/prompt-presets/` that references the `nocturne-memory-*` slots with your character's namespace.
