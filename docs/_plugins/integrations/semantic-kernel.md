# Semantic Kernel (Microsoft) integration

Astrocytes as a kernel plugin for Microsoft Semantic Kernel.

**Module:** `astrocyte.integrations.semantic_kernel`
**Pattern:** Plugin with kernel functions — retain, recall, reflect, forget methods
**Framework dependency:** `semantic-kernel` (optional)

## Install

```bash
pip install astrocyte semantic-kernel
```

## Usage

```python
from astrocyte import Astrocyte
from astrocyte.integrations.semantic_kernel import AstrocytePlugin

brain = Astrocyte.from_config("astrocyte.yaml")
plugin = AstrocytePlugin(brain, bank_id="user-123")

# Register with Semantic Kernel
from semantic_kernel import Kernel
kernel = Kernel()
kernel.add_plugin(plugin, plugin_name="memory")

# Call functions directly
result = await plugin.retain("Calvin prefers dark mode", tags="preference,ui")
result = await plugin.recall("UI preferences")
answer = await plugin.reflect("What are Calvin's preferences?")
```

## Integration pattern

Each method follows the Semantic Kernel `@kernel_function` convention: named method with docstring, type-annotated parameters, returns string.

| Plugin method | Astrocytes call | Returns |
|---|---|---|
| `retain(content, tags?)` | `brain.retain()` | `"Stored memory (id: ...)"` |
| `recall(query, max_results?)` | `brain.recall()` | Formatted list of scored hits |
| `reflect(query)` | `brain.reflect()` | Synthesized answer |
| `forget(memory_ids)` | `brain.forget()` | `"Deleted N memories."` |

## API reference

### `AstrocytePlugin(brain, bank_id, *, include_reflect=True, include_forget=False)`

Methods: `retain()`, `recall()`, `reflect()`, `forget()`, `get_functions()`.

`get_functions()` returns a list of `{"name", "description", "function"}` dicts for programmatic registration.
