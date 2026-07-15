# Frequently Asked Questions

Common questions about Hyper-Extract.

---

## General

### What is Hyper-Extract?

Hyper-Extract is an LLM-powered knowledge extraction framework that transforms unstructured text into structured knowledge graphs, lists, models, and more.

### What can I use it for?

- Research paper analysis
- Knowledge base construction
- Document processing
- Information extraction
- Question-answering systems

### Is it free?

The software is open-source (Apache-2.0). You need to provide your own OpenAI API key for LLM calls.

---

## Installation

### What are the requirements?

- Python 3.11+
- OpenAI API key

### How do I install it?

```bash
pip install hyperextract
```

### Installation fails with "No module named 'hyperextract'"

Try:
```bash
pip install --upgrade hyperextract
```

Or use a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install hyperextract
```

---

## Configuration

### Where do I set my API key?

**Option 1**: CLI
```bash
he config init -k YOUR_API_KEY
```

**Option 2**: Environment variable
```bash
export OPENAI_API_KEY=your-api-key
```

**Option 3**: .env file
```
OPENAI_API_KEY=your-api-key
```

### Can I use a different LLM provider?

Yes, set the base URL:
```bash
he config set llm.base_url https://your-provider.com/v1
```

### Which models are supported?

- OpenAI models (gpt-4o, gpt-4o-mini, etc.)
- OpenAI-compatible APIs whose declared Model Profile passes validation/probe

---

## Usage

### Which template should I use?

See the [How to Choose](../templates/how-to-choose.md) guide or use:
```bash
he list template
```

### How do I process a PDF?

Convert to text first:
```bash
pdftotext document.pdf document.txt
he parse document.txt -t general/graph -l en
```

### Can I process multiple documents?

**Option 1**: Feed incrementally
```bash
he parse doc1.md -t general/graph -o ./ka/ -l en
he feed ./ka/ doc2.md
he feed ./ka/ doc3.md
```

**Option 2**: Process directory
```bash
he parse ./docs/ -t general/graph -o ./ka/ -l en
```

### How do I extract in Chinese?

```bash
he parse doc.md -t general/biography_graph -l zh
```

---

## Performance

### Why is extraction slow?

- Long documents are chunked and processed in parallel
- Each chunk requires an LLM call
- Consider using `--no-index` during batch processing

### How can I speed it up?

1. Use smaller chunk sizes
2. Reduce `max_workers` if hitting rate limits
3. Process documents in parallel (manually)

### Memory issues with large documents?

Process in smaller batches:
```python
for batch in chunks(documents, 5):
    for doc in batch:
        ka.feed_text(doc)
    ka.dump("./checkpoint/")
```

---

## Results

### Where is my data stored?

```
./output/
├── data.json      # Extracted knowledge
├── metadata.json  # Extraction info
└── index/         # Search index
```

### How do I visualize results?

```bash
he show ./output/
```

Or in Python:
```python
# Build index for interactive search/chat in visualization
result.build_index()

result.show()
```

![Interactive Visualization](../../assets/en_show.jpg)

### Can I export to other formats?

```python
import json

# To JSON
json_data = result.data.model_dump_json()

# To dict
data_dict = result.data.model_dump()
```

---

## Troubleshooting

### "API key not found"

Run:
```bash
he config init -k YOUR_API_KEY
```

### "Template not found"

List available templates:
```bash
he list template
```

### "Index not found" error

Build the index:
```bash
he build-index ./output/
```

### Search returns no results

Try:
- Different search terms
- Increase `top_k`: `he search ./ka/ "query" -n 10`
- Check if index is built: `he info ./ka/`

---

## Advanced

### Can I create custom templates?

Yes! See [Custom Templates](../python/guides/custom-templates.md).

### Can I use my own extraction method?

Yes, implement and register:
```python
from hyperextract.methods import register_method

class MyMethod:
    def extract(self, text):
        # Your logic
        pass

register_method("my_method", MyMethod, "graph", "Description")
```

### How do I integrate with my application?

```python
from hyperextract import Template

class MyApp:
    def __init__(self):
        self.ka = Template.create("general/graph", "en")
    
    def process_document(self, text):
        return self.ka.parse(text)
```

---

## Getting More Help

- [GitHub Issues](https://github.com/yifanfeng97/hyper-extract/issues)
- [Troubleshooting Guide](troubleshooting.md)
- [CLI Documentation](../cli/index.md)
- [Python SDK](../python/index.md)
