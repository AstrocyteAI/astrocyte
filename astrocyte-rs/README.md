# Astrocyte (Rust)

This directory is the **Rust** implementation of the Astrocyte service: the same memory, governance, and provider contract as the Python tree, with idiomatic Rust (`async`/tokio where appropriate).

- **Parallel implementation:** [`astrocyte-py`](../astrocyte-py/) (Python / PyPI `astrocyte`).
- **Design docs:** [`docs/`](../docs/README.md), including [`implementation-language-strategy.md`](../docs/_design/implementation-language-strategy.md).

The repository root holds **specification and code together**; this tree is the Rust service, not a separate repository.
