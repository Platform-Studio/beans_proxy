# Beans Proxy

Beans Proxy is a lightweight proxy for LLM API calls that records the token cost by API key.

The intention is to record the token cost of individual tasks.

The mechanism to do this by using psuedo-API keys that are passed into the proxy by whatever runtime environment is being used (e.g. Cline, Copilot CLI, etc). The proxy then passes on the call to the actual LLM API, using an actual API key, and records the token cost against the psuedo-API key.

This works within our current architecture because our orchestration framework has control over the process that starts the agent runtime (e.g. Cline) and can set environment variables for that process. If we are working on task ID 12345, the orchestation framework can run Cline with a URL pointing to the Beans Proxy and an API key of "sk-task-12345". The Beans Proxy will then record all token costs for calls made with that API key against task ID 12345.

After the task is complete, we can query the Beans Proxy for the total token cost for "sk-task-12345" and attribute that cost to task ID 12345 in our database.

This in turn allows us to record each invocation of the agent runtime against a task, and then total up the token costs for each task to get a total cost for that task. This is critical for understanding the cost of running agents and for optimizing our use of LLMs.

## Implementation
Let's build this in Python using FastAPI.

### Persistence
Let's persist token usage to simple files in a directory called "token_usage". Each file will be named after the psuedo-API key (e.g. "sk-task-12345.json") and will contain the total token cost for that key.

If the file doesn't exist when a call is made with a psuedo-API key, we will create it and record the tokens used.

If the file already exists, we will add to the array in the JSON file.

```json
[
  {
    "started_at": "2024-06-01T12:00:00Z",
    "ended_at": "2024-06-01T12:05:00Z",
    "input_tokens": 100,
    "output_tokens": 200,
    "model": "openai/gpt-4o-mini"
  }
]
```

Each entry is an object with at least `started_at` and `ended_at` (ISO 8601 timestamps), and `input_tokens` and `output_tokens` (non-negative integers). On any call that completes upstream — including calls that return a non-2xx status — the `input_tokens` / `output_tokens` values come from the upstream response's `usage` field.

The `model` field is optional and is set to the `model` value from the upstream response body (OpenAI / OpenRouter return the resolved model id on every chat completion, streaming or not). If the upstream response does not include a `model` field, the proxy omits the field from the record rather than recording an empty value.

**Concurrent writes:** Both Cline and Copilot CLI can spawn subagents, so multiple requests may hit the proxy concurrently for the same pseudo-API key. To avoid lost or interleaved writes, the proxy must perform atomic file writes (write to a temp file in the same directory, then `os.replace` it into place) and serialize per-key writes with an in-process lock keyed on the pseudo-API key.

**Failure accounting:** If the upstream call fails (5xx, timeout, connection error, etc.), the proxy must still record the request rather than silently dropping it.

- If the upstream response contains a `usage` field (even alongside a non-2xx status), the entry is written with those token counts and no `error` field.
- If no usage information is available, an entry is still written with `input_tokens: 0`, `output_tokens: 0`, and an `error` field set to a short string describing the failure (e.g. `"upstream_timeout"`, `"upstream_5xx"`, `"connection_error"`). The `ended_at` timestamp is set to the moment the failure was determined.

Example failure record:

```json
{
  "started_at": "2024-06-01T12:00:00Z",
  "ended_at": "2024-06-01T12:05:30Z",
  "input_tokens": 0,
  "output_tokens": 0,
  "error": "upstream_timeout"
}
```

Downstream consumers should treat any record without an `error` field as a successful call, and any record with an `error` field as a failed call whose token counts (if any) should not be summed into spend.

**Cost calculation:** The proxy also records USD input, output, and total costs
when the model has a known OpenRouter price. Pricing is fetched once from
`https://openrouter.ai/api/v1/models` at startup and stored in memory. The
pricing response is preprocessed into a lookup keyed by both `id` and
`canonical_slug`; only base `prompt` and `completion` prices are used. Existing
records are not backfilled. If no usable price is available, the token record
omits cost fields and includes `error: "unknown model/no price"`.

### Configuration
The Beans Proxy will be configured with the actual LLM API endpoint and API key via environment variables. The target URL is the OpenRouter base URL, and the proxy forwards any path the caller supplies under it.

```bash
export BEANS_PROXY_TARGET_URL="https://openrouter.ai/api/v1"
export BEANS_PROXY_TARGET_API_KEY="sk-or-v1-actual-api-key"
```

We can also configure the port that the Beans Proxy runs on via an environment variable:
```bash
export BEANS_PROXY_PORT=8000
```

These environment variables should be setable from a .env file for ease of use in development.

### Path handling
The proxy treats `BEANS_PROXY_TARGET_URL` as a base URL and forwards the full path it receives from the caller to that base. For example, a request to `http://localhost:8000/v1/chat/completions` is forwarded to `${BEANS_PROXY_TARGET_URL}/v1/chat/completions`. Endpoints that have no token cost associated with them (e.g. `/v1/models`) are passed through transparently without recording any usage.

### Streaming
Cline and Copilot CLI both call the LLM using server-sent-event streaming. OpenAI-compatible APIs (including OpenRouter) only include token usage in streaming responses when the caller opts in. The proxy will inject `"stream_options": {"include_usage": true}` into every streaming request body before forwarding it upstream, so that the final `usage` chunk is present and can be recorded.

### Querying usage
The on-disk JSON file in `token_usage/` is the source of truth and can be read directly by other systems. A read endpoint is also exposed for convenience:

- `GET /usage/{pseudo_key}` returns the full JSON array of recorded usages for that pseudo-API key, as stored on disk.
- The pseudo-API key is treated as an arbitrary string; no prefix or format validation is performed.
- The query endpoint is unauthenticated. It should only be exposed on trusted networks.

### Logging
The Beans Proxy will log each call made to it to a single log file (beans_proxy.log), including the psuedo-API key used, the input and output token counts, and the time taken for the call. This will help with debugging and understanding usage patterns.

Other configuration such as model, temperature, effort will juyst be passed through from the caller to the actual LLM API and won't be used by the Beans Proxy itself.

## Using with Cline
Cline can be run to use the Beans Proxy by specifying

Provider: OpenAI Compatible
Base URL: http://localhost:{port}
API key: proxy_key {set by the orchestration framework, e.g. "sk-task-12345"}

## Using with Copilot CLI
Copilot CLI can be configured to use the Beans Proxy by setting the `OPENAI_API_BASE` environment variable to point to the Beans Proxy and using the psuedo-API key as the `OPENAI_API_KEY`. For example:
```bashexport OPENAI_API_BASE="http://localhost:{port}"
export OPENAI_API_KEY="sk-task-12345"
```