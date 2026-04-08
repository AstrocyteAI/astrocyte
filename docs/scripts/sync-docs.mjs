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
  return content.replace(/\]\((\.\.\/[^)]+)\)/g, (full, rel) => {
    const clean = rel.replace(/^\.\.\//, "");
    // Don't convert links that target doc source directories — those are
    // cross-section references handled by docsMarkdownLinksToRoutes.
    const firstSegment = clean.split("/")[0];
    if (DOC_SOURCE_DIRS.has(firstSegment)) return full;
    return `](${FILE_BASE}/${clean})`;
  });
}

/**
 * Resolve ./foo.md and ../_plugins/bar.md relative to the source file, when target is under docs/.
 *
 * Emits relative URLs (e.g., `../architecture-framework/`, `../../plugins/provider-spi/`)
 * instead of root-relative URLs so that links work regardless of the GitHub Pages base path.
 */
function docsMarkdownLinksToRoutes(content, sourceFileAbs, destFileAbs) {
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
    const targetSection = publicSectionFromSourceDir(sourceSection);
    // Build target subdirectory path (for nested files like _plugins/integrations/foo.md)
    const targetSubDir = segments.length > 2 ? segments.slice(1, -1).join("/") : "";
    const targetRoute = targetSubDir
      ? `${targetSection}/${targetSubDir}/${slug}`
      : `${targetSection}/${slug}`;

    // Compute relative path from the destination file's directory to the target route
    const destDir = path.dirname(destFileAbs);
    const destRelToContent = path.relative(contentDocs, destDir).replace(/\\/g, "/");
    const targetFull = path.join(contentDocs, targetRoute);
    const relPath = path.relative(destDir, targetFull).replace(/\\/g, "/");

    return `](${relPath}/)`;
  });
}

/** Known public sections that appear as root-relative links in authored markdown. */
const PUBLIC_SECTIONS = new Set([
  "design", "plugins", "end-user", "tutorials", "eval", "introduction",
]);

/**
 * Convert root-relative doc links like ](/design/foo/) or ](/plugins/integrations/bar/)
 * to relative paths based on the destination file's location within content/docs.
 */
function rootRelativeToRelative(content, destFileAbs) {
  return content.replace(/\]\(\/([\w-]+(?:\/[^)]*)?)\)/g, (full, route) => {
    const topSegment = route.split("/")[0];
    if (!PUBLIC_SECTIONS.has(topSegment)) return full;
    // Strip trailing slash for path computation, then re-add
    const cleanRoute = route.replace(/\/$/, "");
    const destDir = path.dirname(destFileAbs);
    const targetFull = path.join(contentDocs, cleanRoute);
    const relPath = path.relative(destDir, targetFull).replace(/\\/g, "/");
    return `](${relPath}/)`;
  });
}

function transformBody(content, sourceFileAbs, destFileAbs) {
  let result = repoLinksToGitHub(content);
  result = docsMarkdownLinksToRoutes(result, sourceFileAbs, destFileAbs);
  result = rootRelativeToRelative(result, destFileAbs);
  return result;
}

function extractTitle(md) {
  const m = md.match(/^#\s+(.+)$/m);
  return m ? m[1].trim() : "Untitled";
}

/** Matches `id` values in `starlightSidebarTopics` (astro.config.mjs). */
function ensureFrontmatter(raw, titleFallback, sourceFileAbs, topicId, destFileAbs) {
  const m = raw.match(/^#\s+(.+)$/m);
  const title = m ? m[1].trim() : titleFallback;
  const bodyMd = m ? stripFirstH1(raw) : raw;
  const body = transformBody(bodyMd, sourceFileAbs, destFileAbs);
  const fm = `---\ntitle: "${escapeTitle(title)}"\ndraft: false\ntopic: ${topicId}\n---\n\n`;
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
    const destDir = subDir ? path.join(contentDocs, destSection, subDir) : path.join(contentDocs, destSection);
    const destFile = path.join(destDir, ent.name);
    const body = ensureFrontmatter(raw, fb, srcPath, destSection, destFile);
    writeIfChanged(destFile, body);
  }
}

rmGeneratedDirs();

for (const src of ["_design", "_plugins", "_end-user", "_tutorials"]) {
  copyAllMdFromSourceSection(src);
}

const readmePath = path.join(docsDir, "README.md");
const readme = fs.readFileSync(readmePath, "utf8");
let introTitle = extractTitle(readme);
if (introTitle === "Untitled") introTitle = "Astrocyte documentation";
const introDestFile = path.join(contentDocs, "introduction.md");
const introBody =
  `---\ntitle: "${escapeTitle(introTitle)}"\ndraft: false\ntopic: end-user\n---\n\n` +
  transformBody(stripFirstH1(readme), readmePath, introDestFile);
writeIfChanged(introDestFile, introBody);

console.log(
  "sync-docs: wrote introduction.md; _design→design, _plugins→plugins, _end-user→end-user, _tutorials→tutorials",
);
