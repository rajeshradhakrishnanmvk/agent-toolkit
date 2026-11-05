# Unified MCP Server

The `mcp_server.py` provides a single Model Context Protocol (MCP) server that exposes all three agent-toolkit tools (budget guard, handoff checker, and work-claim ledger) through a unified interface.

## Quick Start

```bash
python3 mcp_server.py
```

This starts the MCP server with default ledger paths (`.agent-toolkit/budget-ledger.json` and `.agent-toolkit/work-claims.json` in the current directory).

## Command-Line Options

```bash
python3 mcp_server.py \
  --root /absolute/path/to/project \
  --budget-ledger /absolute/path/to/budget-ledger.json \
  --work-ledger /absolute/path/to/work-claims.json
```

**Arguments:**
- `--root`: Root directory for resolving relative paths (default: current working directory)
- `--budget-ledger`: Path to the budget guard ledger file (default: `.agent-toolkit/budget-ledger.json` relative to `--root`)
- `--work-ledger`: Path to the work-claim ledger file (default: `.agent-toolkit/work-claims.json` relative to `--root`)

## Configure in Claude Desktop

1. Locate your `claude_desktop_config.json`:
   - **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Windows:** `%APPDATA%/Claude/claude_desktop_config.json`
   - **Linux:** `~/.config/Claude/claude_desktop_config.json`

2. Add the agent-toolkit server (replace paths with your absolute paths):

```json
{
  "mcpServers": {
    "agent-toolkit": {
      "command": "python3",
      "args": [
        "/absolute/path/to/agent-toolkit/mcp_server.py",
        "--root",
        "/absolute/path/to/your/project"
      ]
    }
  }
}
```

3. Restart Claude Desktop

## Configure in Cursor

Similar to Claude Desktop, add the server configuration to your Cursor settings and restart the editor.

## Available Tools (11 Total)

### Budget Guard (4 tools)

**1. `budget-guard-open`**
- Open a new budget task with limits
- Parameters:
  - `task` (string, required): Task identifier
  - `max_tokens` (integer, optional): Maximum tokens allowed
  - `max_cost_usd` (number, optional): Maximum cost in USD
  - `max_tool_calls` (integer, optional): Maximum tool calls allowed
  - `max_wall_clock_seconds` (integer, optional): Maximum wall-clock seconds
  - `warn_ratio` (number, optional): Ratio at which to warn (0.0-1.0, default 0.8)

**2. `budget-guard-check`**
- Check if the next step would exceed budget limits
- Returns: `continue`, `warn`, or `stop-and-ask-human`
- Parameters:
  - `task` (string, required): Task identifier
  - `tokens` (integer, optional): Tokens for this step
  - `cost_usd` (number, optional): Cost in USD for this step
  - `tool_calls` (integer, optional): Tool calls for this step

**3. `budget-guard-record`**
- Record the actual spend of a completed step
- Parameters:
  - `task` (string, required): Task identifier
  - `tokens` (integer, optional): Actual tokens spent
  - `cost_usd` (number, optional): Actual cost in USD
  - `tool_calls` (integer, optional): Actual tool calls made
  - `note` (string, optional): Optional note (max 200 chars)

**4. `budget-guard-status`**
- Get the current status of a budget task
- Parameters:
  - `task` (string, required): Task identifier

### Handoff Checker (2 tools)

**5. `handoff-check`**
- Validate a handoff payload (JSON or Markdown with front matter)
- Returns: `ready`, `needs-fixes`, or `blocked`
- Parameters:
  - `payload` (string, required): Handoff payload as JSON string or Markdown with front matter
  - `schema_path` (string, optional): Path to JSON schema file for additional validation

**6. `handoff-template`**
- Get a template for a valid handoff payload
- Parameters: None required

### Work Claim Ledger (5 tools)

**7. `work-claim-take`**
- Claim an item for an owner with a time-to-live (lease) duration
- Parameters:
  - `item` (string, required): Item identifier
  - `owner` (string, required): Owner/agent name
  - `ttl` (integer, optional): Time-to-live in seconds (default 900, max 86400)
  - `note` (string, optional): Optional note (max 200 chars)

**8. `work-claim-renew`**
- Renew a lease on a claimed item
- Parameters:
  - `item` (string, required): Item identifier
  - `owner` (string, required): Current owner/agent name
  - `ttl` (integer, optional): New time-to-live in seconds

**9. `work-claim-release`**
- Release a claimed item back to the pool
- Parameters:
  - `item` (string, required): Item identifier
  - `owner` (string, required): Current owner/agent name

**10. `work-claim-done`**
- Mark a claimed item as completed
- Parameters:
  - `item` (string, required): Item identifier
  - `owner` (string, required): Current owner/agent name
  - `note` (string, optional): Optional completion note (max 200 chars)

**11. `work-claim-list`**
- List work claims with optional filtering by state
- Parameters:
  - `state` (string, optional): Filter by claim state (`open`, `active`, `expired`, `stale`, `done`, `all`; default: `open`)
  - `limit` (integer, optional): Maximum results to return (default 50, max 100)

## Protocol Details

- **Transport:** Stdio (one JSON-RPC 2.0 message per line)
- **Protocol Version:** 2024-11-05
- **No third-party dependencies:** Uses Python standard library only
- **No network calls:** All operations are local

## Examples

### Example 1: Budget Guard Workflow

```json
{"jsonrpc":"2.0","method":"tools/call","params":{"name":"budget-guard-open","arguments":{"task":"fix-bug","max_tokens":2000,"max_cost_usd":0.05}},"id":1}
{"jsonrpc":"2.0","method":"tools/call","params":{"name":"budget-guard-check","arguments":{"task":"fix-bug","tokens":500}},"id":2}
{"jsonrpc":"2.0","method":"tools/call","params":{"name":"budget-guard-record","arguments":{"task":"fix-bug","tokens":500,"note":"analysis"}},"id":3}
```

### Example 2: Work Claim Workflow

```json
{"jsonrpc":"2.0","method":"tools/call","params":{"name":"work-claim-take","arguments":{"item":"task-1","owner":"agent-a","ttl":600}},"id":1}
{"jsonrpc":"2.0","method":"tools/call","params":{"name":"work-claim-list","arguments":{"state":"all"}},"id":2}
{"jsonrpc":"2.0","method":"tools/call","params":{"name":"work-claim-done","arguments":{"item":"task-1","owner":"agent-a"}},"id":3}
```

## Testing

Run the demo to see all tools in action:

```bash
python3 demo_mcp_server.py
```

This demo:
1. Initializes the server
2. Lists all available tools
3. Opens a budget task
4. Records a step
5. Checks budget for the next step
6. Claims a work item
7. Completes the demo

## File Locations

- **Ledger files:** Created in `.agent-toolkit/` directory by default (relative to `--root`)
  - Budget ledger: `.agent-toolkit/budget-ledger.json`
  - Work claims: `.agent-toolkit/work-claims.json`
- **Lock files:** `.agent-toolkit/*.lock` (for process synchronization)

These paths are gitignored by default.

## Error Handling

- Invalid JSON-RPC messages result in error code `-32700` (parse error)
- Unknown tools result in error code `-32601` (method not found)
- Server errors result in error code `-32603` (internal error)

All error messages include a description of what went wrong.

## Performance

- **No blocking I/O:** All operations are synchronous but fast
- **File-based ledgers:** Simple JSON files with atomic writes
- **Process-safe:** File locking prevents concurrent write conflicts
- **Memory efficient:** Keeps only recent events (100 for budget guard, up to 100 for work claims)

## Integration with Your Harness

The MCP server is designed to be started once and run continuously, processing requests from Claude, Cursor, or your custom client. It maintains state in JSON ledgers that survive restarts.

For agent builders using MCP clients, the unified server provides a single integration point for all budget, handoff, and work-claim operations.
