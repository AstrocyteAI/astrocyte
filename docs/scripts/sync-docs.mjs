#!/usr/bin/env node
/**
 * Copy specs from docs/{_design,_plugins,_end-user,_tutorials}/ into Starlight content.
 * Underscore-prefixed folders are authoring layout; published routes stay /design/, /plugins/, …
 * Sidebar order is defined in astro.config.mjs (explicit items), not by filename.
 *
 * Relative markdown links under docs/ → site routes /{public-section}/{slug}/.
 * README.md → introduction.md
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const docsDir = path.resolve(__dirname, "..");
const contentDocs = path.join(docsDir, "src/content/docs");

const FILE_BASE =
  process.env.DOCS_GITHUB_FILE_BASE || "https://github.com/AstrocyteAI/astrocyte/blob/main";

/** Authoring folder (under docs/) → URL path segment (no underscore). */
const SOURCE_TO_PUBLIC = {
  _design: "design",
  _plugins: "plugins",
  "_end-user": "end-user",
  _tutorials: "tutorials",
};

const DOC_SOURCE_DIRS = new Set(Object.keys(SOURCE_TO_PUBLIC));

function publicSectionFromSourceDir(sourceDir) {
  return SOURCE_TO_PUBLIC[sourceDir] ?? sourceDir;
}

function escapeTitle(s) {
  return s.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

function stripFirstH1(md) {
  return md.replace(/^#\s+.+\n+/, "");
}

function repoLinksToGitHub(content) {
  return content.replace(/\]\((\.\.\/[^)]+)\)/g, (_, rel) => {
    const clean = rel.replace(/^\.\.\//, "");
    return `](${FILE_BASE}/${clean})`;
  });
}

/**
 * Resolve ./foo.md and ../_plugins/bar.md relative to the source file, when target is under docs/.
 */
function docsMarkdownLinksToRoutes(content, sourceFileAbs) {
  return content.replace(/\]\((\.\.?\/[^)]*\.md)\)/g, (full, href) => {
    const abs = path.resolve(path.dirname(sourceFileAbs), href);
    const rel = path.relative(docsDir, abs);
    if (rel.startsWith("..") || path.isAbsolute(rel)) {
      return full;
    }
    const norm = rel.replace(/\\/g, "/");
    const segments = norm.split("/").filter(Boolean);
    if (segments.length < 2) {
      return full;
    }
    const sourceSection = segments[0];
    const file = segments[segments.length - 1];
    if (!file.endsWith(".md")) {
      return full;
    }
    if (!DOC_SOURCE_DIRS.has(sourceSection)) {
      return full;
    }
    const slug = file.replace(/\.md$/, "");
    const publicSection = publicSectionFromSourceDir(sourceSection);
    return `](/${publicSection}/${slug}/)`;
  });
}

function transformBody(content, sourceFileAbs) {
  return repoLinksToGitHub(docsMarkdownLinksToRoutes(content, sourceFileAbs));
}

function extractTitle(md) {
  const m = md.match(/^#\s+(.+)$/m);
  return m ? m[1].trim() : "Untitled";
}

function ensureFrontmatter(raw, titleFallback, sourceFileAbs) {
  const m = raw.match(/^#\s+(.+)$/m);
  const title = m ? m[1].trim() : titleFallback;
  const bodyMd = m ? stripFirstH1(raw) : raw;
  const body = transformBody(bodyMd, sourceFileAbs);
  const fm = `---\ntitle: "${escapeTitle(title)}"\ndraft: false\n---\n\n`;
  return fm + body;
}

function writeIfChanged(dest, data) {
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  if (fs.existsSync(dest) && fs.readFileSync(dest, "utf8") === data) return;
  fs.writeFileSync(dest, data);
}

function rmGeneratedDirs() {
  const legacySpec = path.join(contentDocs, "spec");
  if (fs.existsSync(legacySpec)) fs.rmSync(legacySpec, { recursive: true, force: true });

  for (const d of ["design", "plugins", "end-user", "tutorials"]) {
    const p = path.join(contentDocs, d);
    if (fs.existsSync(p)) fs.rmSync(p, { recursive: true, force: true });
  }
}

function copyAllMdFromSourceSection(sourceDirName, subDir = "") {
  const srcDir = path.join(docsDir, sourceDirName, subDir);
  const destSection = publicSectionFromSourceDir(sourceDirName);
  if (!fs.existsSync(srcDir)) return;
  for (const ent of fs.readdirSync(srcDir, { withFileTypes: true })) {
    if (ent.isDirectory()) {
      // Recurse into subdirectories (e.g., _plugins/integrations/)
      copyAllMdFromSourceSection(sourceDirName, path.join(subDir, ent.name));
      continue;
    }
    if (!ent.isFile() || !ent.name.endsWith(".md")) continue;
    const srcPath = path.join(srcDir, ent.name);
    const raw = fs.readFileSync(srcPath, "utf8");
    let fb = extractTitle(raw);
    if (fb === "Untitled") fb = ent.name.replace(/\.md$/, "").replace(/-/g, " ");
    const body = ensureFrontmatter(raw, fb, srcPath);
    const destDir = subDir ? path.join(contentDocs, destSection, subDir) : path.join(contentDocs, destSection);
    writeIfChanged(path.join(destDir, ent.name), body);
  }
}

rmGeneratedDirs();

for (const src of ["_design", "_plugins", "_end-user", "_tutorials"]) {
  copyAllMdFromSourceSection(src);
}

const readmePath = path.join(docsDir, "README.md");
const readme = fs.readFileSync(readmePath, "utf8");
let introTitle = extractTitle(readme);
if (introTitle === "Untitled") introTitle = "Astrocytes documentation";
const introBody =
  `---\ntitle: "${escapeTitle(introTitle)}"\ndraft: false\n---\n\n` +
  transformBody(stripFirstH1(readme), readmePath);
writeIfChanged(path.join(contentDocs, "introduction.md"), introBody);

console.log(
  "sync-docs: wrote introduction.md; _design→design, _plugins→plugins, _end-user→end-user, _tutorials→tutorials",
);
