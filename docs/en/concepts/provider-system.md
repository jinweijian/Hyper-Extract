# Provider System

Hyper-Extract separates the provider transport from model capabilities. OpenAI Chat-compatible endpoints share one adapter, while each named Model Profile explicitly declares structured-output modes, token parameter mapping, reasoning layout, embedding limits, and recovery budgets. Compatibility is based on a validated Profile or Probe result, not on a provider or model name guess.

The conservative `openai-compatible-default` profile reads `OPENAI_MODEL`, `OPENAI_BASE_URL`, and `OPENAI_API_KEY` and enables `text_json` only. Embeddings use the independent `EMBEDDING_*` route.

```bash
he model validate --profile openai-compatible-default
he model show --profile openai-compatible-default
he model probe --profile production-profile --file model-profiles.toml
```

`validate` is local-only. `probe` performs small real requests and caches evidence under `~/.he/probes/`; profiles with `probe_required = true` cannot start without current successful evidence.

---

## Verified Model Compatibility

### Cloud API

| Platform | Model | `json_schema` Support | Compatible | Notes |
|----------|-------|:---------------------:|:----------:|-------|
| **OpenAI** | gpt-4o / gpt-4o-mini / gpt-5 | ✅ | ✅ | Native support, recommended |
| **Anthropic** | claude-opus-4-8 / claude-sonnet-4-6 / claude-haiku-4-5 | ✅ (tool calling) | ✅ | LLM only — no embeddings API (pair with an OpenAI-compatible embedder). Needs `hyperextract[anthropic]` |
| **Alibaba Bailian** | qwen-plus / qwen-turbo / qwen3.6-plus / deepseek-r1 | ✅ | ✅ | Works out of the box |
| **Alibaba Bailian** | qwen-max / deepseek-v3 | ❌ | ❌ | Only `json_object`; `json_schema` not supported |

> **Bailian users**: Both qwen-max and deepseek-v3 do not support `json_schema`. If you hit `messages must contain the word 'json'` or get non-JSON output, switch to qwen-plus, qwen-turbo, or deepseek-r1. See [Issue #24](https://github.com/yifanfeng97/Hyper-Extract/issues/24).

### Local Deployment

| Service | Model | Quantization | VRAM | Verified |
|---------|-------|-------------|------|----------|
| LLM | **Qwen3.5-9B** | GPTQ-Marlin 4bit | ~8GB | ✅ AutoList / AutoGraph |
| Embedding | **BAAI/bge-m3** | None | ~2GB | ✅ Semantic search |

### Cloud Embedding

| Platform | Model | Dimensions | Verified |
|----------|-------|------------|----------|
| **OpenAI** | text-embedding-3-small | 1536 | ✅ |
| **Alibaba Bailian** | text-embedding-v4 | 1024 | ✅ |

---

## Quick Start by Platform

```python
from hyperextract import create_client

# OpenAI
llm, emb = create_client("openai", api_key="sk-xxx")

# Bailian
llm, emb = create_client("bailian", api_key="sk-xxx")

# Anthropic (Claude) — LLM only; Anthropic has no embeddings API, so
# pair it with an OpenAI-compatible embedder.
# Keys: ANTHROPIC_API_KEY (or CLAUDE_API_KEY) for the LLM, OPENAI_API_KEY for embeddings.
llm, emb = create_client(
    llm="anthropic",  # default model: claude-opus-4-8 (override with "anthropic:<model>")
    embedder="openai:text-embedding-3-small",
)

# Local vLLM
llm, emb = create_client(
    llm="vllm:Qwen3.5-9B@http://localhost:8000/v1",
    embedder="vllm:bge-m3@http://localhost:8001/v1",
    api_key="dummy",
)
```

Full configuration options: [Provider Configuration Guide](../python/guides/provider-configuration.md).

---

## vLLM Deployment

### Start LLM Service

```bash
vllm serve /path/to/qwen3.5-9b-gptq-marlin \
  --served-model-name Qwen/Qwen3.5-9B \
  --trust-remote-code \
  --quantization gptq_marlin \
  --dtype bfloat16 \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.90 \
  --default-chat-template-kwargs '{"enable_thinking": false}' \
  --port 8000 \
  --api-key dummy
```

### Start Embedding Service

```bash
vllm serve BAAI/bge-m3 \
  --task embed \
  --dtype float16 \
  --max-model-len 8192 \
  --port 8001
```

### Docker

```bash
docker run --runtime nvidia --gpus all \
  --ipc=host \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -p 8000:8000 \
  vllm/vllm-openai:latest \
  --model Qwen/Qwen3.5-9B \
  --trust-remote-code
```

---

## Recommendations

**Use non-thinking models for local vLLM deployment.**

When deploying locally, thinking models (e.g. Qwen3.5 thinking mode) output `<think>...</think>` tags, which conflict with constrained decoding's requirement for JSON from the first token. Disable thinking mode when deploying Qwen3.5-9B:

```bash
--default-chat-template-kwargs '{"enable_thinking": false}'
```

**DeepSeek-R1 via Bailian is verified to work** — Bailian filters out `<think>` tags on the backend, so AutoGraph and `with_structured_output` work correctly. If accessing DeepSeek-R1 through other channels, thinking output may still cause issues.

**Prefer GPTQ-Marlin over AWQ** for quantization — AWQ has compatibility issues with vLLM 0.21.0.

---

## References

- [vLLM Structured Outputs](https://docs.vllm.ai/en/latest/features/structured_outputs/)
- [XGrammar Paper](https://arxiv.org/abs/2411.15100)
- [Issue #21: Domestic Model Support](https://github.com/yifanfeng97/Hyper-Extract/issues/21)
