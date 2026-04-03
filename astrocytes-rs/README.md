# Astrocytes (Rust)

This directory is the **Rust** implementation of the Astrocytes service: the same memory, governance, and provider contract as the Python tree, with idiomatic Rust (`async`/tokio where appropriate).

- **Parallel implementation:** [`astrocytes-py`](../astrocytes-py/) (Python / PyPI `astrocytes`).
- **Design docs:** [`docs/`](../docs/README.md), including [`13-implementation-language-strategy.md`](../docs/13-implementation-language-strategy.md).

The repository root holds **specification and code together**; this tree is the Rust service, not a separate repository.
