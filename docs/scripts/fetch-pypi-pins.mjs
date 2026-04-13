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

function isStableVersion(version) {
  return !/(?:a|b|rc|dev)\d*$/i.test(version);
}

function parseVersion(version) {
  let raw = String(version).trim();
  let epoch = 0;
  const epochParts = raw.split("!");
  if (epochParts.length === 2) {
    epoch = Number.parseInt(epochParts[0], 10) || 0;
    raw = epochParts[1];
  }

  raw = raw.split("+", 1)[0];

  let post = 0;
  const postMatch = raw.match(/^(.*?)(?:\.post(\d+))?$/i);
  if (postMatch) {
    raw = postMatch[1];
    post = Number.parseInt(postMatch[2] || "0", 10) || 0;
  }

  const parts = raw.split(".").map((part) => Number.parseInt(part, 10) || 0);
  return { epoch, parts, post };
}

function compareVersions(a, b) {
  const pa = parseVersion(a);
  const pb = parseVersion(b);

  if (pa.epoch !== pb.epoch) return pa.epoch - pb.epoch;

  const len = Math.max(pa.parts.length, pb.parts.length);
  for (let i = 0; i < len; i += 1) {
    const diff = (pa.parts[i] || 0) - (pb.parts[i] || 0);
    if (diff !== 0) return diff;
  }

  return pa.post - pb.post;
}

async function latestFromPypi(name) {
  const url = `https://pypi.org/pypi/${encodeURIComponent(name)}/json`;
  const res = await fetch(url, {
    headers: { "Accept": "application/json", "User-Agent": "astrocyte-docs-fetch-pypi-pins/1.0" },
  });
  if (!res.ok) {
    throw new Error(`${name}: HTTP ${res.status}`);
  }
  const data = await res.json();
  const releases = data?.releases;
  if (!releases || typeof releases !== "object") {
    throw new Error(`${name}: missing releases`);
  }
  const stable = Object.entries(releases)
    .filter(([version, files]) => isStableVersion(version) && Array.isArray(files) && files.length > 0)
    .map(([version]) => version)
    .sort(compareVersions);
  const version = stable.at(-1);
  if (!version) {
    throw new Error(`${name}: no stable release found`);
  }
  return version;
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
