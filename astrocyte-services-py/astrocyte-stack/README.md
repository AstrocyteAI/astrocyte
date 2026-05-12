# astrocyte-stack

Convenience meta-package for Astrocyte's default production stack.

```bash
pip install astrocyte-stack
```

That's it. Equivalent to:

```bash
pip install "astrocyte[default]"
# which itself resolves to:
pip install astrocyte astrocyte-postgres
```

## Why a separate package

`astrocyte-stack` exists so the recommended install is one short copy-paste,
not three packages or a square-bracket extras string. The trade-off:

- Library / framework consumers (`pip install astrocyte`) get a thin install
  with only the SPI types and in-memory backends. ~11 packages.
- Production / quick-start consumers (`pip install astrocyte-stack`) get the
  Postgres-backed brain that the docs assume. ~18 packages.
- The default stack can evolve (e.g. swapping the default vector backend
  later) without `astrocyte` itself growing dependencies.

## What's inside

Only a dependency declaration. There is no `astrocyte_stack` Python API.
For actual usage, `import astrocyte` — see
[the quick-start](https://AstrocyteAI.github.io/astrocyte/end-user/quick-start/).

## Choosing a different default

If you want a non-Postgres backend, install `astrocyte` directly with the
relevant per-adapter extra:

```bash
pip install "astrocyte[qdrant]"          # Qdrant vector store
pip install "astrocyte[neo4j]"           # Neo4j graph store
pip install "astrocyte[elasticsearch]"   # Elasticsearch document store
```

Or browse the [ecosystem and packaging guide](https://AstrocyteAI.github.io/astrocyte/plugins/ecosystem-and-packaging/)
for the full matrix of optional dependencies.

## License

Apache-2.0. See [LICENSE](../../LICENSE).
