# astrocyte-gateway (Helm)

Minimal chart for the HTTP gateway. Build and push an image from [`../../astrocyte-gateway/Dockerfile`](../../astrocyte-gateway/Dockerfile), then install with your registry and secrets:

```bash
helm install gw ./astrocyte-gateway \
  --set image.repository=YOUR_REGISTRY/astrocyte-gateway \
  --set image.tag=YOUR_TAG \
  --set-json 'env=[{"name":"DATABASE_URL","valueFrom":{"secretKeyRef":{"name":"astrocyte","key":"database-url"}}}]'
```

Or use `values.yaml` / `extraEnvFrom` to mount a Secret with `DATABASE_URL` and optional `ASTROCYTE_CONFIG_PATH`.
