#!/usr/bin/env node
/**
 * Copy design specs from this directory (docs/) into Starlight's content tree.
 * Sources: ./NN-*.md and ./README.md  →  ./src/content/docs/spec/ and introduction.md
 * Run before `pnpm dev` / `pnpm build`.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
/** Root of the docs/ package (parent of scripts/) */
const docsDir = path.resolve(__dirname, "..");
const outSpec = path.join(docsDir, "src/content/docs/spec");
const outIntro = path.join(docsDir, "src/content/docs/introduction.md");

const FILE_BASE =
  process.env.DOCS_GITHUB_FILE_BASE || "https://github.com/AstrocyteAI/astrocytes/blob/main";

function escapeTitle(s) {
  return s.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

function stripFirstH1(md) {
  return md.replace(/^#\s+.+\n+/, "");
}

/** Repo-relative links (../foo) → GitHub blob URLs */
function repoLinksToGitHub(content) {
  return content.replace(/\]\((\.\.\/[^)]+)\)/g, (_, rel) => {
    const clean = rel.replace(/^\.\.\//, "");
    return `](${FILE_BASE}/${clean})`;
  });
}

/** ./NN-name.md → /spec/NN-name/ (respects Astro base at runtime) */
function specLinksToRoutes(content) {
  return content.replace(/\]\(\.\/(\d{2}-[^)]+\.md)\)/g, (_, file) => {
    const slug = file.replace(/\.md$/, "");
    return `](/spec/${slug}/)`;
  });
}

function transformBody(content) {
  return specLinksToRoutes(repoLinksToGitHub(content));
}

function extractTitle(md) {
  const m = md.match(/^#\s+(.+)$/m);
  return m ? m[1].trim() : "Untitled";
}

fs.mkdirSync(outSpec, { recursive: true });

for (const name of fs.readdirSync(docsDir)) {
  if (!/^\d{2}-.+\.md$/.test(name)) continue;
  const raw = fs.readFileSync(path.join(docsDir, name), "utf8");
  const title = extractTitle(raw);
  const body = transformBody(stripFirstH1(raw));
  const fm = `---\ntitle: "${escapeTitle(title)}"\ndraft: false\n---\n\n`;
  fs.writeFileSync(path.join(outSpec, name), fm + body);
}

const readme = fs.readFileSync(path.join(docsDir, "README.md"), "utf8");
const introTitle = extractTitle(readme);
const introBody = transformBody(stripFirstH1(readme));
fs.writeFileSync(
  outIntro,
  `---\ntitle: "${escapeTitle(introTitle)}"\ndraft: false\n---\n\n` + introBody,
);

console.log("sync-docs: wrote src/content/docs/introduction.md and src/content/docs/spec/*.md");
