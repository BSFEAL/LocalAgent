# Providers

LocalAgent never talks to a vendor SDK. Every model call goes through one small interface:

```python
provider.complete(messages, tools=None, *, temperature=None, max_tokens=None, seed=None) -> Completion
```

Adapters translate that to each vendor's API. Providers are named with a spec string, **`<provider>:<model>`**.
Everything after the first colon is the model name, so `ollama:qwen3:32b` works.

## Built-in providers

| Spec | Service | Credentials / endpoint |
|---|---|---|
| `openai:<model>` | OpenAI | `OPENAI_API_KEY`; `OPENAI_BASE_URL` to override |
| `anthropic:<model>` | Anthropic | `ANTHROPIC_API_KEY`; `ANTHROPIC_BASE_URL` to override |
| `gemini:<model>` | Google Gemini | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |
| `vllm:<model>` | local vLLM / SGLang | `VLLM_BASE_URL` (default `http://127.0.0.1:8000/v1`), optional `VLLM_API_KEY` |
| `ollama:<model>` | local Ollama | `OLLAMA_BASE_URL` (default `http://127.0.0.1:11434/v1`) |
| `lmstudio:<model>` | local LM Studio | `LMSTUDIO_BASE_URL` (default `http://127.0.0.1:1234/v1`) |
| `openrouter:<vendor>/<model>` | OpenRouter | `OPENROUTER_API_KEY` |
| `compat:<model>` | any other OpenAI-compatible server (Together, Groq, Fireworks, DeepSeek, llama.cpp…) | `COMPAT_BASE_URL`, `COMPAT_API_KEY` |
| `mock:<name>` | scripted offline model (tests, demos) | none |
| `echo:<name>` | echoes the last user message | none |

What every adapter handles:

* **Tool calling**, translated to each vendor's format: OpenAI `tool_calls`, Anthropic `tool_use`/`tool_result`
  blocks, Gemini `functionCall`/`functionResponse`.
* **Reasoning text**: Anthropic `thinking` blocks, Gemini thoughts, and vLLM `reasoning_content` are kept on
  the message, so recorded trajectories (and training data) include them.
* **Retries**: requests that fail with 408/409/429/5xx/529 or a connection error are retried with exponential backoff.
* **JSON answers**: `complete_json` doesn't depend on any vendor's JSON mode. It extracts JSON from fenced or
  surrounding text, and asks again if the reply isn't valid JSON.

## Routing

Each agent asks the router for its model by **role**. Role names are dotted:

| Role | Used by |
|---|---|
| `coder.<agent_id>` | each agent of the coder workflow |
| `review.<agent_id>` | each reviewer (`review.security`, `review.risk_classifier`…) |
| `deploy` | deploy agent |
| `perf` | perf engineer |
| `sevbot`, `sevbot.<agent_id>` | Sevbot |
| `mass`, `mass.optimizer`, `mass.judge` | MASS (the model being improved, its workflow optimizer, its judge) |

If a role has no model set, the router drops the last part of the name and tries again
(`review.security` → `review` → `default`). So one line can configure a whole stage:

```toml
[providers]
default = "ollama:qwen3:32b"

[providers.routes]
review = "anthropic:claude-opus-5-5"          # every reviewer
"review.cloud" = "gemini:gemini-3-pro"         # except this one
sevbot = "openai:gpt-5.5"
```

A single agent in a workflow can also pin itself to a role with `"route": "my-role"`.

> **MASS note.** The paper relies on *one* model playing three parts. It runs the stage's agents
> (looked up through that stage's roles, e.g. `coder.*`), rewrites the workflow (`mass.optimizer`) and
> judges the results (`mass.judge`). To match the paper, all of these should resolve to the same model.
> The simplest way is to set only `default`. A newly trained model replaces `default`, so a stage
> pinned to another model through `routes` will not pick up the trained model.

## Per-provider options

Options under `[providers.options.<provider>]` are passed to that adapter's constructor:

```toml
[providers.options.vllm]
base_url = "http://gpu-box:8000/v1"
timeout = 1200

[providers.options.openai]
extra_body = { reasoning_effort = "high" }   # merged into every request body

[providers.options.anthropic]
max_tokens = 16000
```

Common options: `base_url`, `api_key`, `timeout`, `temperature`, `max_tokens`. OpenAI-compatible
adapters also take `extra_body`, `extra_headers` and `api_key_env`.

## Using providers from Python

```python
from localagent.providers import create_provider, ProviderRouter, Message

p = create_provider("anthropic:claude-opus-5-5", max_tokens=4000)
reply = p.complete([Message("system", "Be terse."), Message("user", "2+2?")])
print(reply.message.content, reply.usage)

router = ProviderRouter("ollama:qwen3:32b", {"review.security": p})   # specs or provider objects
```

## Adding a provider

Subclass `Provider`, implement `complete`, and register it:

```python
from localagent.providers import Provider, Completion, Message, Usage, register_provider

class MyCloud(Provider):
    name = "mycloud"
    def complete(self, messages, tools=None, *, temperature=None, max_tokens=None, seed=None):
        ...  # call your API, convert to/from Message / ToolCall
        return Completion(Message("assistant", text), Usage(in_tok, out_tok), model=self.model)

register_provider("mycloud", MyCloud)       # now "mycloud:model-x" works everywhere
```

If your service speaks the OpenAI Chat Completions format, you don't need code. Use `compat:` and set
`COMPAT_BASE_URL`, or register a named preset the same way the built-in `vllm` and `ollama` are.
