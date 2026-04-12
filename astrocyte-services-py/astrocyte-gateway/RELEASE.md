# Releasing the gateway image (GHCR)

## First-time setup (org admin)

1. **Branch protection:** follow **[`BRANCH-PROTECTION.md`](./BRANCH-PROTECTION.md)** (require the **`CI`** check on `main`).
2. **Image attestations:** the publish workflow pushes **SLSA-style build provenance** to the registry (GitHub **artifact attestations**, Sigstore-backed). Verify after a release (needs [GitHub CLI](https://cli.github.com/) `gh`):

   ```bash
   gh attestation verify oci://ghcr.io/OWNER/REPO/astrocyte-gateway:v0.2.0 --repo OWNER/REPO
   ```

   Replace `OWNER/REPO` and the tag. Public repos on current GitHub plans can use attestations per [GitHub docs](https://docs.github.com/en/actions/security-guides/using-artifact-attestations-to-establish-provenance-for-builds); private repos may need Enterprise for attestations.

## Cut a release

1. Ensure **`main`** is green (including gateway matrix + pgvector jobs).
2. Tag from the repo root, e.g. `git tag v0.2.0 && git push origin v0.2.0`.
3. Open **Actions → Publish astrocyte-gateway image to GHCR** and confirm the run succeeds (tests, then build + push).
4. In **Packages** (org or repo), open **`astrocyte-gateway`**, set **visibility** (public for OSS pulls without auth), and verify tags **`v0.2.0`** and **`latest`**.

## Smoke-pull

```bash
docker pull ghcr.io/astrocyteai/astrocyte/astrocyte-gateway:v0.2.0
docker run --rm -p 8080:8080 ghcr.io/astrocyteai/astrocyte/astrocyte-gateway:v0.2.0
curl -fsS http://127.0.0.1:8080/live
```

(Replace `astrocyteai/astrocyte` with your `owner/repo` if different.)
