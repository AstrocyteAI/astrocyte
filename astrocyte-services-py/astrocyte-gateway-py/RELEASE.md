# Releasing the gateway image (GHCR)

## First-time setup (org admin)

1. **Branch protection:** follow **[`BRANCH-PROTECTION.md`](./BRANCH-PROTECTION.md)** (require the **`CI`** check on `main`).
2. **Image attestations:** the publish workflow pushes **SLSA-style build provenance** to the registry (GitHub **artifact attestations**, Sigstore-backed). Verify after a release (needs [GitHub CLI](https://cli.github.com/) `gh`):

   ```bash
   gh attestation verify oci://ghcr.io/OWNER/REPO/astrocyte-gateway-py:v0.8.0 --repo OWNER/REPO
   ```

   Each platform image also carries an **SPDX SBOM** attestation, attached to that platform's digest rather than the multi-arch index (an index has no filesystem to inventory). Verify one by its platform digest, or let `cosign` resolve the platform from the tag:

   ```bash
   docker buildx imagetools inspect ghcr.io/OWNER/REPO/astrocyte-gateway-py:v0.8.0   # lists each platform's digest
   gh attestation verify oci://ghcr.io/OWNER/REPO/astrocyte-gateway-py@sha256:<platform digest> --repo OWNER/REPO \
     --predicate-type https://spdx.dev/Document/v2.3
   cosign download attestation --platform linux/arm64 \
     --predicate-type https://spdx.dev/Document/v2.3 ghcr.io/OWNER/REPO/astrocyte-gateway-py:v0.8.0
   ```

   `gh attestation verify` checks the digest the tag points at (the index), so pass the platform digest for SBOMs. `cosign download sbom` does not apply: it reads cosign's deprecated SBOM attachments, not attestations.

   Replace `OWNER/REPO` and the tag. Public repos on current GitHub plans can use attestations per [GitHub docs](https://docs.github.com/en/actions/security-guides/using-artifact-attestations-to-establish-provenance-for-builds); private repos may need Enterprise for attestations.

## Cut a release

1. Ensure **`main`** is green (including gateway matrix + pgvector jobs) and **`CHANGELOG.md`** lists **`v0.8.0`** (see root **`RELEASING.md`**).
2. Tag from the repo root, e.g. `git tag -a v0.8.0 -m "Release v0.8.0" && git push origin v0.8.0`.
3. Open **Actions → Release** ([`release.yml`](../../.github/workflows/release.yml)) and confirm the run succeeds: PyPI **`astrocyte`** → **`astrocyte-postgres`**, then gateway image build + push.
4. In **Packages** (org or repo), open **`astrocyte-gateway-py`**, set **visibility** (public for OSS pulls without auth), and verify tags **`v0.8.0`** and **`latest`**.

## Smoke-pull

```bash
docker pull ghcr.io/astrocyteai/astrocyte/astrocyte-gateway-py:v0.8.0
docker run --rm -p 8080:8080 ghcr.io/astrocyteai/astrocyte/astrocyte-gateway-py:v0.8.0
curl -fsS http://127.0.0.1:8080/live
```

(Replace `astrocyteai/astrocyte` with your `owner/repo` if different.)
