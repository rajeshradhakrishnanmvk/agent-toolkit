# agent-toolkit

One small, single-file tool for AI agent builders, every week.

Built in public by Rajesh (Kochi). Each tool is announced on LinkedIn with a short screen recording. The project page is [https://rajeshradhakrishnanmvk.github.io/agent-toolkit/](https://rajeshradhakrishnanmvk.github.io/agent-toolkit/).

| Week | Tool | Status |
| --- | --- | --- |
| 1 | [Budget guard](#week-1--budget-guard) | Live |
| 2 | Handoff checker | Coming soon |
| 3 | Work-claim ledger | Coming soon |

The page is static HTML in `/docs`. GitHub Pages: branch `main`, folder `/docs`.

## Week 1 — Budget guard

Agents (crews, coding agents, MCP-tool bots) burn tokens, dollars, and tool calls on long tasks. The human usually finds out afterwards.

`budget_guard.py` lets the harness declare a budget for one task and check in before each costly step. The answer is `continue`, `warn`, or `stop-and-ask-human`. Totals live in a JSON file, so they survive a restart.

It is one file, standard library only. Use it as a Python module, a CLI, or a local MCP server.

The guard reports a decision. It does not kill your process. When the decision is `stop-and-ask-human`, your harness should refuse the next step and show `ask_human` to the person.

### Decisions

| Decision | Meaning |
| --- | --- |
| `continue` | Every configured limit is under the warn line. |
| `warn` | At least one limit is at or past the warn ratio (default 80%). Still ok to proceed. |
| `stop-and-ask-human` | The step would pass a limit, or a limit is already past. Do not spend more until a human says so. |

Any one of tokens, US dollars, tool calls, or wall-clock can trip the guard. Unset limits are not checked. Wall-clock is real time since `open`, including idle time, and it keeps moving across restarts.

`check` does not spend. Two checks do not stack: each one is a hypothetical on the committed total. `record` writes what actually happened, even if that total is over budget.

### 30-second quickstart

```bash
python3 budget_guard.py open fix-flaky-test \
  --max-tokens 2500 --max-cost-usd 0.02 --max-tool-calls 4

python3 budget_guard.py check fix-flaky-test --tokens 600 --tool-calls 1
python3 budget_guard.py record fix-flaky-test --tokens 600 --tool-calls 1 --note read_failures
python3 budget_guard.py status fix-flaky-test
```

`check`, `record`, and `status` exit `0` on continue or warn, `3` on stop-and-ask-human, and `1` on a real error. Add `--json` for the machine-readable decision.

```bash
python3 demo.py
```

That demo is the week-1 recording: a four-step loop that is allowed, warned, then stopped. Step 4 is not written to the ledger.

### As a library

```python
from budget_guard import STOP_AND_ASK, BudgetGuard

guard = BudgetGuard(".agent-toolkit/budget-ledger.json")
guard.open_task(
    "fix-flaky-test",
    max_tokens=2500,
    max_cost_usd=0.02,
    max_tool_calls=4,
)

decision = guard.check("fix-flaky-test", tokens=600, tool_calls=1)
if decision["decision"] == "warn":
    print(decision["reason"])  # near the limit; still ok to proceed
if decision["decision"] == STOP_AND_ASK:
    raise SystemExit(decision["ask_human"])

# ...do the step, then record what it actually cost...
guard.record("fix-flaky-test", tokens=600, tool_calls=1, note="read_failures")
```

`open_task` is the method name because `open` is a Python builtin. The CLI and MCP tool are both called `open` / `budget_open`.

### As an MCP server

```bash
python3 budget_guard.py serve --ledger /absolute/path/to/budget-ledger.json
```

Stdio transport, one JSON-RPC message per line (the same shape Cursor and Claude Desktop speak). Tools: `budget_open`, `budget_check`, `budget_record`, `budget_status`.

```json
{
  "mcpServers": {
    "budget-guard": {
      "command": "python3",
      "args": [
        "/absolute/path/to/agent-toolkit/budget_guard.py",
        "serve",
        "--ledger",
        "/absolute/path/to/agent-toolkit/.agent-toolkit/budget-ledger.json"
      ]
    }
  }
}
```

Use an absolute `--ledger`. The client's working directory is not a reliable place for the file.

### Ledger

Default path: `.agent-toolkit/budget-ledger.json`, or `$BUDGET_GUARD_LEDGER`.

The file is pretty-printed JSON. A sibling `budget-ledger.json.lock` serializes readers and writers. Writes go to a temp file and replace the ledger, so a crash mid-write keeps the previous totals. The recent 100 events stay in the file; older ones are counted in `events_dropped` and the totals still include them.

This repo gitignores `.agent-toolkit/`. If you point the ledger somewhere else, don't commit it. Notes you pass to `record` are stored in plain text.

### Pricing

There is no built-in price list. Providers change prices, and the same model name can cost different amounts on different hosts. Pass `cost_usd` yourself.

To turn token counts into dollars, pass rates you looked up. The numbers below are an example, not a quote:

```bash
python3 budget_guard.py cost \
  --input-tokens 2000 --output-tokens 500 \
  --usd-per-million-input 3 --usd-per-million-output 15
```

```python
from budget_guard import estimate_cost_usd

estimate_cost_usd(2000, 500, usd_per_million_input=3, usd_per_million_output=15)
# 0.0135, from those rates only
```

The CLI, the library, and the MCP server do not make network calls.

### Tests

```bash
python3 -m unittest discover -s tests -v
```

Requires Python 3.9+. No third-party packages.

## License

MIT. Copyright 2026 Rajesh Radhakrishnan.
