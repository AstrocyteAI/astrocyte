#!/usr/bin/env node
/**
 * Resolve latest stable versions from PyPI JSON API and write src/data/pypi-install-pins.json
 * for the landing page install snippets. On failure (offline CI, rate limit), copies
 * pypi-install-pins.sample.json instead.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const docsDir = path.resolve(__dirname, "..");
const outFile = path.join(docsDir, "src/data/pypi-install-pins.json");
const sampleFile = path.join(docsDir, "src/data/pypi-install-pins.sample.json");

const PACKAGES = ["astrocyte", "astrocyte-pgvector"];

async function latestFromPypi(name) {
  const url = `https://pypi.org/pypi/${encodeURIComponent(name)}/json`;
  const res = await fetch(url, {
    headers: { "Accept": "application/json", "User-Agent": "astrocyte-docs-fetch-pypi-pins/1.0" },
  });
  if (!res.ok) {
    throw new Error(`${name}: HTTP ${res.status}`);
  }
  const data = await res.json();
  const v = data?.info?.version;
  if (!v || typeof v !== "string") {
    throw new Error(`${name}: missing info.version`);
  }
  return v;
}

function writeSampleFallback(reason) {
  console.warn(`fetch-pypi-pins: ${reason} — using ${path.basename(sampleFile)}`);
  fs.mkdirSync(path.dirname(outFile), { recursive: true });
  fs.copyFileSync(sampleFile, outFile);
}

async function main() {
  if (!fs.existsSync(sampleFile)) {
    console.error(`fetch-pypi-pins: missing ${sampleFile}`);
    process.exitCode = 1;
    return;
  }

  try {
    const versions = {};
    for (const pkg of PACKAGES) {
      versions[pkg] = await latestFromPypi(pkg);
    }
    const payload = {
      ...versions,
      fetchedAt: new Date().toISOString(),
      source: "https://pypi.org/",
    };
    fs.mkdirSync(path.dirname(outFile), { recursive: true });
    fs.writeFileSync(outFile, `${JSON.stringify(payload, null, 2)}\n`);
    console.log(
      "fetch-pypi-pins: wrote",
      outFile,
      `(${PACKAGES.map((p) => `${p}==${versions[p]}`).join(", ")})`,
    );
  } catch (err) {
    writeSampleFallback(err instanceof Error ? err.message : String(err));
  }
}

await main();
