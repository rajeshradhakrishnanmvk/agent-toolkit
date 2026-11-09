# Harness v0

A small agent harness that runs in the browser. GitHub Pages serves this folder, so the page, the loop, the tools, and the profiles are the same files.

Open the harness page, type an API key, and send a message. The key stays in that browser tab. It is sent only to the endpoint in the form. Nothing in this folder is a hosted chat service, and there is no Node command for running the agent.

The loop is inspired by ZeroClaw: a short agent loop, a pluggable provider, tools, memory-ready profiles, and supervised autonomy with approval gates. v0 is the browser slice of that idea.

## Quick start

1. Open the harness on GitHub Pages, or serve the `docs` folder with any static file server and open the `harness` path.
2. Leave the demo session selected. Autonomy defaults to `supervised`.
3. Type an API key. It is kept in `sessionStorage` for this tab and is not written into the trace.
4. Ask: `What time is it and how much budget is left?`
5. The model should call `clock`, then `budget` status, then answer from those results. A medium or high risk tool, such as `budget` record, stops and shows Approve and Deny on the page.

The public endpoint allows about 10 requests a minute. A 429 waits and retries. A 401, a 5xx, or a network failure is shown as text and is not retried into a hidden loop.

Default endpoint and model, from `profiles/global.json`:

- `https://chat.srv1536099.hstgr.cloud/v1`
- `Llama-3.2-3B-Instruct`

Node is only for the test runner: `node docs/harness/test.js`. The same tests run in the browser at `test.html`.

## Profile schema

Profiles are JSON. `kind` is `global`, `soul`, or `session`.

```json
{
  "id": "demo",
  "kind": "session",
  "name": "Demo",
  "instructions": "What this layer should do.",
  "config": { "soulId": "general", "goals": "", "notes": "" }
}
```

Configuration merges global, then soul, then session. Nested objects merge. Arrays replace. `null` does not erase a value. Instructions are concatenated in that same order. A session's `goals` and `notes` are appended after its instructions.

`config.soulId` picks the soul. `config.autonomy` is `readonly`, `supervised`, or `full`. `config.budget` holds `maxTokens`, `maxToolCalls`, `maxCostUsd`, and `warnRatio` (default 0.8). `contextTokens` defaults to 2048 and `maxReplyTokens` to 256. The prompt must fit in `contextTokens - maxReplyTokens`. Older turns are dropped first. Token estimates are `ceil(characters / 4)`, not a model tokenizer.

Shipped files:

- `profiles/global.json`
- `profiles/souls/general.json`
- `profiles/sessions/demo.json`
- `profiles/manifest.json`

## Tool interface

A tool is a plain object. Add one to the array passed into `runAgent`.

| Field | Meaning |
| --- | --- |
| `name` | Tool name the model puts in the fenced JSON. |
| `description` | One or two sentences, included in the system prompt. |
| `schema` | JSON Schema for the arguments. v0 documents the shape; it does not run a schema validator. |
| `risk` | `"low"`, `"medium"`, or `"high"`. A function `(args) => level` is allowed when one action is riskier than another. |
| `run(args, ctx)` | Async function. Return JSON-serializable data. `ctx` has `now`, `taskId`, `budget`, `approvals`, and `fetch`. |

The model calls a tool with one fenced block:

```tool
{"name":"clock","args":{}}
```

The parser takes the first valid `tool` block. A broken tool block is treated as a plain answer. At most one tool runs for each model reply. The loop stops after 3 model calls.

v0 tools:

| Tool | Risk | What it does |
| --- | --- | --- |
| `clock` | low | Current UTC time. |
| `budget` | low for `estimate`, `check`, and `status`; medium for `record` | Same decisions as `budget_guard.py`: `continue`, `warn`, `stop-and-ask-human`. `check` does not spend. `record` writes what happened. The harness refuses the next model call when a check says stop. |
| `ask_human` | low | `ask`, `status`, and `list` on the approval ledger. `answer` is not available to the model. |

Ledgers live in `localStorage` (`agent-toolkit.harness.budget.v1` and `agent-toolkit.harness.approvals.v1`). They follow the Python ledgers' rules closely enough for one task at a time: one pending approval per task, an answer must match the stored question and action, and an approval does not raise the budget by itself.

## Autonomy

| Level | Behavior |
| --- | --- |
| `readonly` | Only low-risk tools run. A medium or high tool returns a block message and is not executed. |
| `supervised` | Default. Low-risk tools run. A medium or high tool files an `ask_human` request and pauses until Approve or Deny on this page. |
| `full` | Every registered tool runs without a pause. |

Approve and Deny record the decision in the ledger and then continue the same turn. Deny does not run the tool. The ledger is a local record, not a check of who clicked the button.

## Trace

Each step is a JSON object: time, kind, prompt size, token estimate, tool name, gate decision, and duration. The page keeps the trace in memory. Download trace saves `harness-trace.jsonl`. The API key is not a field in that file, and it is stripped if an error message would have contained it.

## Tests

```bash
node docs/harness/test.js
```

Or open `test.html` on the same static server. The suite covers profile merge, tool-call parsing, one tool per reply, the 3-iteration cap, supervised pause and resume, a readonly block, a budget stop, the trace, 429 backoff, and the demo question against a stubbed provider.

## Roadmap

- v1: `handoff` and `work_claim` tools, with a two-agent demo.
- v2: file memory with keyword recall.
- v3: workspace-bounded file and shell tools, with approval for writes. Node only.
- v4: MCP client to `mcp_server.py` over stdio.
- v5: a golden-prompt eval set.
- v6: one inbound channel (webhook).
- v7: an optional scheduled job on the VPS.
- v8: a multi-session router.

## Limits of v0

The page talks to an OpenAI-compatible `/v1/chat/completions` endpoint. Tool calls are fenced JSON because native tool calling on the 3B model is unreliable. The loop will not run two tools from one reply, and it will not make a fourth model call. The budget and the approval ledger are per browser, not the Python processes. Reset budget clears the spend for the selected session. A paused turn is kept in `sessionStorage` so a reload of this tab can still Approve or Deny.
