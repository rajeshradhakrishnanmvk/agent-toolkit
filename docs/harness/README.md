# Harness v1

A small agent harness that runs in the browser. GitHub Pages serves this folder, so the page, the loop, the tools, and the profiles are the same files.

v1 adds two tools, `handoff` and `work_claim`, and a two-agent demo. Planner and Writer take turns in one session. The loop underneath is still the v0 browser slice: short turns, a pluggable provider, tools, and approval gates.

Open the harness page, type an API key, and send a message. The key stays in that browser tab. It is sent only to the endpoint in the form. Nothing in this folder is a hosted chat service, and there is no Node command for running the agent.

## Quick start

1. Open the harness on GitHub Pages, or serve the `docs` folder with any static file server and open the `harness` path.
2. Leave the demo session selected. Autonomy defaults to `supervised`.
3. Type an API key. It is kept in `sessionStorage` for this tab and is not written into the trace.
4. Ask: `What time is it and how much budget is left?`
5. The model should call `clock`, then `budget` status, then answer from those results. A medium or high risk tool, such as `budget` record or `work_claim` claim, stops and shows Approve and Deny on the page.

Two agents, on the same page:

1. Select **Two agents**, or press **Run two agents** (it selects that session).
2. Planner claims `demo-task`, writes a handoff, and the run releases that claim. Writer claims the item, checks the handoff, and answers.
3. A handoff that is `blocked` or otherwise not `ready` stops the run. The reason is the checker summary and the problem list, in the thread and in the alert under the composer.
4. The whole demo makes at most 4 model calls. Add `?stub=1` to the page address to run that script locally. The stub does not send an API key.

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
- `profiles/souls/planner.json`
- `profiles/souls/writer.json`
- `profiles/sessions/demo.json`
- `profiles/sessions/handoff.json`
- `profiles/manifest.json`

Soul prompts for Planner and Writer are a few sentences. The two-agent run puts only `handoff` and `work_claim` in their tool list, so the system prompt stays small enough for a 2048-token context.

## Tool interface

A tool is a plain object. Add one to the array passed into `runAgent`.

| Field | Meaning |
| --- | --- |
| `name` | Tool name the model puts in the fenced JSON. |
| `description` | One or two sentences, included in the system prompt. |
| `schema` | JSON Schema for the arguments. The harness documents the shape; it does not run a schema validator. |
| `risk` | `"low"`, `"medium"`, or `"high"`. A function `(args) => level` is allowed when one action is riskier than another. |
| `run(args, ctx)` | Async function. Return JSON-serializable data. `ctx` has `now`, `taskId`, `budget`, `approvals`, `claims`, `handoffBox`, `files`, and `fetch`. |

The model calls a tool with one fenced block:

```tool
{"name":"clock","args":{}}
```

The parser takes the first valid `tool` block. A broken tool block is treated as a plain answer. At most one tool runs for each model reply. The single-agent loop stops after 3 model calls. The two-agent demo stops after 4 model calls in total.

| Tool | Risk | What it does |
| --- | --- | --- |
| `clock` | low | Current UTC time. |
| `budget` | low for `estimate`, `check`, and `status`; medium for `record` | Same decisions as `budget_guard.py`: `continue`, `warn`, `stop-and-ask-human`. `check` does not spend. `record` writes what happened. The harness refuses the next model call when a check says stop. |
| `ask_human` | low | `ask`, `status`, and `list` on the approval ledger. `answer` is not available to the model. |
| `handoff` | low | `check` validates a handoff document. `template` returns a blank one. Status is `ready`, `needs-fixes`, or `blocked`, with the same problem list as `handoff_checker.py`. |
| `work_claim` | low for `list`; medium for `claim`, `release`, `heartbeat`, and `done` | One owner holds an item until the lease ends. `heartbeat` renews that lease. A second owner is `busy` while the lease is live. After `expires_at` the claim is stale and the next owner gets `taken-over`. |

`handoff` follows the Python checker: required fields (`goal`, `acceptance_criteria`, `owner`, `constraints`, `budget_left`), an optional schema that can add fields but cannot drop those, vague acceptance criteria, unresolved questions, leftover TODO/TBD/FIXME, an exhausted budget (`blocked`), a size cap (`blocked`, default 16 KiB), and cited paths. Paths are checked against `ctx.files`, a map of paths that exist in this page. There is no disk.

`work_claim` follows `work_claim.py`: `taken`, `taken-over`, `held`, `busy`, `renewed`, `released`, `done`, `not-owner`, `absent`, and `closed`. The default lease is 900 seconds. The maximum is 24 hours. The ledger is `localStorage` key `agent-toolkit.harness.claims.v1`. A single-tab queue replaces the Python file lock.

Ledgers for budget and approvals stay in `localStorage` (`agent-toolkit.harness.budget.v1` and `agent-toolkit.harness.approvals.v1`). They follow the Python ledgers' rules closely enough for one task at a time: one pending approval per task, an answer must match the stored question and action, and an approval does not raise the budget by itself.

## Autonomy

| Level | Behavior |
| --- | --- |
| `readonly` | Only low-risk tools run. A medium or high tool returns a block message and is not executed. |
| `supervised` | Default. Low-risk tools run. A medium or high tool files an `ask_human` request and pauses until Approve or Deny on this page. |
| `full` | Every registered tool runs without a pause. |

The two-agent demo runs claim, release, and heartbeat without a pause, including when the page selector is `supervised`. That is what keeps the demo inside 4 model calls. Approve and Deny still apply to a normal Run. Deny does not run the tool. The ledger is a local record, not a check of who clicked the button.

## Two-agent run

One session, two souls, one claim ledger, one handoff document:

1. Planner calls `work_claim` `claim` on `demo-task`.
2. Planner calls `handoff` `check` with the document. The run then releases Planner's claim.
3. Writer calls `work_claim` `claim`.
4. Writer calls `handoff` `check`. The document is the one Planner stored. If `ok_to_hand_off` is false, the run stops and the page shows the summary and each problem. If it is true, the sentence above the tool fence is the answer.

Release is a ledger call, not a fifth model call. Both souls are fields on the JSONL trace (`soul`) and labels on the run view. When the run finishes, the demo releases its own item if Planner or Writer still holds it, so the next press can claim it. It does not take a live claim owned by someone else.

## Trace

Each step is a JSON object: time, kind, soul, prompt size, token estimate, tool name, gate decision, and duration. The page keeps the trace in memory. Download trace saves `harness-trace.jsonl`. The API key is not a field in that file, and it is stripped if an error message would have contained it.

## Tests

```bash
node docs/harness/test.js
```

Or open `test.html` on the same static server. The suite covers the v0 cases (profile merge, tool-call parsing, one tool per reply, the 3-iteration cap, supervised pause and resume, a readonly block, a budget stop, the trace, 429 backoff, and the demo question against a stubbed provider) plus handoff ready / needs-fixes / blocked, a schema, the blank template, a claim conflict, a stale-claim takeover, heartbeat, and the two-agent run. That run asserts the model is called 4 times or fewer.

## Roadmap

- v2: IndexedDB keyword memory.
- v3: a virtual file workspace with write approval.
- v4: an MCP-style bridge.
- v5: golden-prompt evals.
- v6: one inbound channel, if GitHub Pages allows it.
- v7: scheduled runs.
- v8: a multi-session router.

## Limits of v1

The page talks to an OpenAI-compatible `/v1/chat/completions` endpoint. Tool calls are fenced JSON because native tool calling on the 3B model is unreliable. The single-agent loop will not run two tools from one reply, and it will not make a fourth model call. The two-agent demo will not make a fifth.

The handoff checker matches `handoff_checker.py` on the decisions it can make in a browser: required fields, the schema subset, vague criteria, questions, TODO/TBD/FIXME, budget, size, and cited paths. Cited paths use an in-memory map, not the computer's disk, and a schema is an object on the call, not a file path. A bare filename is not read from disk. Markdown front matter is the same small subset (scalars, lists, one level of maps).

The claim ledger matches `work_claim.py` for take, renew (heartbeat), release, done, and list, including `busy`, `held`, stale takeover, and `closed`. It does not lock a file across processes or browser tabs. Two tabs can race. Timestamps are ISO-8601 UTC (`Z`), not the Python `+00:00` form. The owner id is a label, not a secret.

The demo releases Planner's claim itself after the handoff is stored, so that step is not a model call. A reply that is not the expected tool stops the run with a reason. It does not retry, because the endpoint allows about 10 requests a minute and the cap is 4 calls.

The budget, approval, and claim ledgers are per browser, not the Python processes. Reset budget clears the spend for the selected session. A paused turn is kept in `sessionStorage` so a reload of this tab can still Approve or Deny. A claim left behind by a closed tab stays until its lease ends (default 15 minutes) or the next demo press clears an item still held by Planner or Writer.

`?stub=1` replays the scripted demo in this browser. It does not call the endpoint and it does not read the API key.
