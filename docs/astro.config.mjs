import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";

const owner = process.env.GITHUB_REPOSITORY_OWNER || "AstrocyteAI";
const repo = process.env.GITHUB_REPOSITORY?.split("/")?.[1];
const site = `https://${owner}.github.io`;
const base = repo ? `/${repo}/` : "/";

export default defineConfig({
  site,
  base,
  integrations: [
    starlight({
      title: "Astrocytes",
      description:
        "Open-source memory framework between agents and storage: retain/recall/synthesize, pluggable backends, governance and observability.",
      social: [
        {
          icon: "github",
          label: "GitHub",
          href: "https://github.com/AstrocyteAI/astrocytes",
        },
      ],
      sidebar: [
        {
          label: "Overview",
          items: [{ label: "Introduction", link: "/introduction/" }],
        },
        {
          label: "Specification",
          autogenerate: { directory: "spec" },
        },
      ],
      customCss: ["./src/styles/custom.css"],
    }),
  ],
});
