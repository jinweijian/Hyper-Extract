<div align="center">

<a href="https://yifanfeng97.github.io/Hyper-Extract/latest/">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo/logo-horizontal-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/logo/logo-horizontal.svg">
  <img alt="Hyper-Extract Logo" src="docs/assets/logo/logo-horizontal.svg" width="600">
</picture>
</a>

<br/>
<br/>

**Smart Knowledge Extraction CLI**

**Transform documents into structured knowledge with one command.**

[📖 English Version](./README.md) · [中文版](./README_ZH.md)

<!-- Status ribbon -->
<p align="center">
  <a href="https://trendshift.io/repositories/25420" target="_blank">
    <img src="https://trendshift.io/api/badge/repositories/25420" alt="Trendshift" width="250" height="55">
  </a>
</p>

<p align="center">
  <a href="https://pypi.org/project/hyperextract/">
    <img src="https://img.shields.io/pypi/v/hyperextract?style=for-the-badge&logo=pypi&logoColor=white&labelColor=1a1a2e&color=3776ab" alt="PyPI Version">
  </a>
  <a href="https://python.org">
    <img src="https://img.shields.io/badge/python-3.11%2B-3776ab?style=for-the-badge&logo=python&logoColor=white&labelColor=1a1a2e" alt="Python Version">
  </a>
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/license-Apache%202.0-06b6d4?style=for-the-badge&labelColor=1a1a2e" alt="License">
  </a>
  <a href="https://yifanfeng97.github.io/Hyper-Extract/latest/">
    <img src="https://img.shields.io/badge/docs-online-3b82f6?style=for-the-badge&logo=readthedocs&logoColor=white&labelColor=1a1a2e" alt="Docs">
  </a>
  <a href="https://github.com/yifanfeng97/hyper-extract/stargazers">
    <img src="https://img.shields.io/github/stars/yifanfeng97/hyper-extract?style=for-the-badge&logo=github&labelColor=1a1a2e&color=facc15" alt="GitHub Stars">
  </a>
</p>

<br/>

> **"Stop reading. Start understanding."**  
> *"告别文档焦虑，让信息一目了然"*

<br/>

<img src="docs/assets/hero.jpg" alt="Hero & Workflow" width="800" style="max-width: 100%;">

<br/>
</div>

## 📰 What's New

<!-- News snippets are derived from the latest merged PRs. Update as new releases land. -->

- **🔌 MCP Server** — Query your knowledge abstracts from Claude Desktop and IDE agents with `he-mcp`. *(PR #40)*
- **🧠 Anthropic Claude Support** — Use `claude-opus-4-8`, `claude-sonnet-4-6`, and `claude-haiku-4-5` directly as your LLM provider. *(PR #38)*
- **📝 Obsidian Export** — Turn any graph into an Obsidian vault with Markdown notes linked by `[[wikilinks]]`. *(PR #37)*
- **🧹 `he clean`** — Remove a KA's index or the whole knowledge abstract in one command. *(PR #39)*
- **🔧 Reliability Fixes** — True mean for multi-chunk embeddings, capped OpenAI-compatible batch sizes, and resolved multi-word `llm_*` merge strategies. *(PRs #35, #36, #41)*

See the full changelog in the [GitHub releases](https://github.com/yifanfeng97/hyper-extract/releases).

Hyper-Extract is an intelligent, LLM-powered knowledge extraction and evolution framework. It radically simplifies transforming highly unstructured texts into persistent, predictable, and strongly-typed **Knowledge Abstracts**. It effortlessly extracts information into a wide spectrum of formats—ranging from simple **Collections** (Lists/Sets) and **Pydantic Models**, to complex **Knowledge Graphs**, **Hypergraphs**, and even **Spatio-Temporal Graphs**.

## ✨ Core Features

| | |
|:---|:---|
| 🔷 **8 Knowledge Structures** | From simple Lists to advanced Graphs, Hypergraphs, and Spatio-Temporal Graphs |
| 🧠 **10+ Extraction Engines** | GraphRAG, LightRAG, Hyper-RAG, KG-Gen, and more — ready to use |
| 📝 **80+ YAML Templates** | Zero-code extraction across Finance, Legal, Medical, TCM, Industry, and General domains |
| 🔄 **Incremental Evolution** | Feed new documents anytime to expand and refine your knowledge base |
| 📤 **Obsidian Export** | Turn any extracted graph into an Obsidian vault — Markdown notes linked by `[[wikilinks]]` |

## 🎯 What Can You Do With It?

<details>
<summary><b>📄 Researcher — Turn papers into knowledge graphs</b></summary>
<br>

Feed a 20-page academic paper, get an interactive graph of key concepts, authors, and citations.

```bash
he parse paper.pdf -t general/academic_graph -o ./paper_kb/
he show ./paper_kb/
```

</details>

<details>
<summary><b>🏦 Financial Analyst — Extract entities from earnings reports</b></summary>
<br>

Automatically identify companies, executives, financial metrics, and their relationships from unstructured reports.

```bash
he parse earnings.md -t finance/earnings_graph -o ./finance_kb/
he search ./finance_kb/ "What are the key risk factors?"
```

</details>

<details>
<summary><b>🔒 Local Deployment — Keep data on-premise with vLLM</b></summary>
<br>

Run Qwen3.5-9B + bge-m3 locally via vLLM. No data leaves your machine.

```python
from hyperextract import create_client
llm, emb = create_client(
    llm="vllm:Qwen3.5-9B@http://localhost:8000/v1",
    embedder="vllm:bge-m3@http://localhost:8001/v1",
    api_key="dummy",
)
```

</details>

## 🚀 Supported Platforms & Models

Hyper-Extract relies on the LLM's structured output capability (`json_schema` or Function Calling).

| Platform | Verified Models |
|----------|-----------------|
| **OpenAI** | gpt-4o, gpt-4o-mini, gpt-5 |
| **Anthropic** | claude-opus-4-8, claude-sonnet-4-6, claude-haiku-4-5 |
| **阿里云百炼** | qwen-plus, qwen-turbo, deepseek-r1 |
| **Local vLLM** | Qwen3.5-9B (GPTQ-Marlin) |

**Embedding models** (semantic search) use the OpenAI Embeddings transport when the endpoint passes the declared Profile constraints for batch size, token limits, input form, dimensions, and response alignment. Verified routes include `text-embedding-3-small`, `text-embedding-v4` (Bailian), and `bge-m3` (local vLLM).

> **Anthropic note:** Claude is used for the **LLM** (set `ANTHROPIC_API_KEY`). Anthropic has no embeddings API, so pair it with an OpenAI-compatible embedder:
> ```python
> from hyperextract import create_client
> llm, emb = create_client(llm="anthropic", embedder="openai:text-embedding-3-small")
> ```
> Requires the extra: `pip install 'hyperextract[anthropic]'`.

> 📖 Full guide: [Provider System & Local Model Support](https://yifanfeng97.github.io/Hyper-Extract/latest/concepts/provider-system/)

## ⚡ 30-Second Quick Start

```bash
# Install
uv tool install hyperextract

# Configure API key
he config init -k YOUR_OPENAI_API_KEY

# Extract knowledge from a document
he parse examples/en/tesla.md -t general/biography_graph -o ./output/ -l en

# Query it
he search ./output/ "What are Tesla's major achievements?"

# Visualize
he show ./output/

# Export to an Obsidian vault (Markdown notes + [[wikilinks]])
he export obsidian ./output/ -o ./vault/
```

<details>
<summary><b>🐍 Python API</b> (click to expand)</summary>
<br>

```bash
uv pip install hyperextract
```

```python
from hyperextract import Template

ka = Template.create("general/biography_graph")

with open("examples/en/tesla.md") as f:
    result = ka.parse(f.read())

result.show()
```

> 🔗 More examples: [examples/en](./examples/en/)

</details>

## 📈 Why Hyper-Extract?

| Feature | GraphRAG | LightRAG | KG-Gen | ATOM | **Hyper-Extract** |
| :------ | :------: | :------: | :----: | :--: | :---------------: |
| Knowledge Graph | ✅ | ✅ | ✅ | ✅ | ✅ |
| Temporal Graph | ✅ | ❌ | ❌ | ✅ | ✅ |
| Spatial Graph | ❌ | ❌ | ❌ | ❌ | ✅ |
| Hypergraph | ❌ | ❌ | ❌ | ❌ | ✅ |
| Domain Templates | ❌ | ❌ | ❌ | ❌ | ✅ |
| Interactive CLI | ✅ | ❌ | ❌ | ❌ | ✅ |
| Multi-language | ✅ | ❌ | ❌ | ❌ | ✅ |

## 🧩 Supported Knowledge Structures

From simple to complex — pick the right structure for your data:

<img src="docs/assets/autotypes.jpg" alt="Knowledge Structures Matrix" width="750" style="max-width: 100%;">

**Example — AutoGraph visualization:**

<img src="docs/assets/en_show.jpg" alt="AutoGraph Visualization" width="750" style="max-width: 100%; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1);">

<details>
<summary><b>📋 What's under the hood? (Architecture & Templates)</b></summary>
<br>

Hyper-Extract follows a **three-layer architecture**:

- **Auto-Types** — 8 strongly-typed data structures (Model, List, Set, Graph, Hypergraph, Temporal Graph, Spatial Graph, Spatio-Temporal Graph)
- **Methods** — Extraction algorithms: KG-Gen, GraphRAG, LightRAG, Hyper-RAG, Cog-RAG, and more
- **Templates** — 80+ presets across 6 domains. Zero-code setup.

<img src="docs/assets/arch.jpg" alt="Architecture" width="750" style="max-width: 100%;">

**Template example (Graph type):**

```yaml
language: en
name: Knowledge Graph
type: graph
tags: [general]
description: 'Extract entities and their relationships.'
output:
  entities:
    fields:
    - name: name
      type: str
    - name: type
      type: str
    - name: description
      type: str
  relations:
    fields:
    - name: source
      type: str
    - name: target
      type: str
    - name: type
      type: str
identifiers:
  entity_id: name
  relation_id: '{source}|{type}|{target}'
```

- [Browse all 80+ templates](./hyperextract/templates/presets/)
- [Create custom templates](./hyperextract/templates/DESIGN_GUIDE.md)

</details>

## 📚 Documentation & Resources

| Resource | Link |
| :------- | :--- |
| Full Documentation | [yifanfeng97.github.io/Hyper-Extract](https://yifanfeng97.github.io/Hyper-Extract/latest/) |
| CLI Guide | [Command-line interface](https://yifanfeng97.github.io/Hyper-Extract/latest/cli/) |
| Provider System | [Model compatibility & local deployment](https://yifanfeng97.github.io/Hyper-Extract/latest/concepts/provider-system/) |
| Template Gallery | [80+ presets](./hyperextract/templates/presets/) |
| Examples | [Working code](./examples/) |

## 🔌 MCP Server

Expose your knowledge abstracts to MCP-capable assistants (Claude Desktop, IDE agents) via the [Model Context Protocol](https://modelcontextprotocol.io) — read + export only.

```bash
pip install 'hyperextract[mcp]'
he-mcp        # stdio MCP server
```

Tools: `list_templates`, `info`, `search`, `ask` (RAG), `export_obsidian`. Full guide: [MCP Server docs](https://yifanfeng97.github.io/Hyper-Extract/latest/mcp/).

## 🤝 Contributing & License

Contributions are welcome! Please submit [Issues](https://github.com/yifanfeng97/hyper-extract/issues) and [PRs](https://github.com/yifanfeng97/hyper-extract/pulls).  
Licensed under **Apache-2.0**.

## 🔒 Security

This project has been security assessed by [MseeP.ai](https://mseep.ai/app/yifanfeng97-hyper-extract).

## AtomGit Mirror

AtomGit mirror - a synchronized AtomGit mirror of Agent Reach for easier access and cloning in China. Hosted on AtomGit: https://atomgit.com/yifanfeng97/Hyper-Extract
