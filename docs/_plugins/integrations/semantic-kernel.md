# Semantic Kernel (Microsoft) integration

Astrocyte as a kernel plugin for Microsoft Semantic Kernel.

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

| Plugin method | Astrocyte call | Returns |
|---|---|---|
| `retain(content, tags?)` | `brain.retain()` | `"Stored memory (id: ...)"` |
| `recall(query, max_results?)` | `brain.recall()` | Formatted list of scored hits |
| `reflect(query)` | `brain.reflect()` | Synthesized answer |
| `forget(memory_ids)` | `brain.forget()` | `"Deleted N memories."` |

## End-to-end example

A Semantic Kernel agent that builds up knowledge about a project:

```python
import asyncio
from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion
from astrocyte import Astrocyte
from astrocyte.integrations.semantic_kernel import AstrocytePlugin

brain = Astrocyte.from_config("astrocyte.yaml")
plugin = AstrocytePlugin(brain, bank_id="project-alpha")

kernel = Kernel()
kernel.add_plugin(plugin, plugin_name="memory")
kernel.add_service(AzureChatCompletion(
    deployment_name="gpt-4o",
    endpoint=endpoint,
    api_key=api_key,
))

async def main():
    # Store project facts
    await plugin.retain(
        "Project Alpha uses FastAPI with PostgreSQL and Redis caching",
        tags="architecture,tech-stack",
    )
    await plugin.retain(
        "Deployment target is AWS EKS with Helm charts",
        tags="architecture,deployment",
    )

    # Recall specific knowledge
    results = await plugin.recall("database setup", max_results=3)
    print(results)

    # Synthesize across all project knowledge
    answer = await plugin.reflect("Summarize the project architecture")
    print(answer)

asyncio.run(main())
```

## Using with planner

Semantic Kernel's planner can auto-select memory functions:

```python
from semantic_kernel.planners import FunctionCallingStepwisePlanner

planner = FunctionCallingStepwisePlanner(kernel)
result = await planner.invoke(
    "What do we know about the deployment architecture? "
    "Check memory first, then summarize."
)
```

## API reference

### `AstrocytePlugin(brain, bank_id, *, include_reflect=True, include_forget=False)`

Methods: `retain()`, `recall()`, `reflect()`, `forget()`, `get_functions()`.

`get_functions()` returns a list of `{"name", "description", "function"}` dicts for programmatic registration.
