import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";
import starlightSidebarTopics from "starlight-sidebar-topics";

const owner = process.env.GITHUB_REPOSITORY_OWNER || "AstrocyteAI";
const repo = process.env.GITHUB_REPOSITORY?.split("/")?.[1];
const site = `https://${owner}.github.io`;
const base = repo ? `/${repo}/` : "/";

/** Explicit sidebar order (filenames without .md → routes). */
const topicItems = {
  endUser: [
    { label: "Introduction", link: "/introduction/" },
    { label: "Quick Start", link: "/end-user/quick-start/" },
    { label: "Production-grade reference server", link: "/end-user/production-grade-http-service/" },
  ],
  plugins: [
    { label: "Provider SPI", link: "/plugins/provider-spi/" },
    { label: "Outbound transport", link: "/plugins/outbound-transport/" },
    { label: "Multimodal LLM SPI", link: "/plugins/multimodal-llm-spi/" },
    { label: "Ecosystem & packaging", link: "/plugins/ecosystem-and-packaging/" },
    { label: "Agent framework middleware", link: "/plugins/agent-framework-middleware/" },
  ],
  design: [
    { label: "Neuroscience & vocabulary", link: "/design/neuroscience-astrocytes/" },
    { label: "Design principles", link: "/design/design-principles/" },
    { label: "Architecture framework", link: "/design/architecture-framework/" },
    { label: "Identity & external policy", link: "/design/identity-and-external-policy/" },
    {
      label: "Presentation layer & multimodal services",
      link: "/design/presentation-layer-and-multimodal-services/",
    },
    { label: "Policy layer", link: "/design/policy-layer/" },
    { label: "Use-case profiles", link: "/design/use-case-profiles/" },
    { label: "Built-in pipeline", link: "/design/built-in-pipeline/" },
    { label: "Implementation language strategy", link: "/design/implementation-language-strategy/" },
    { label: "Multi-bank orchestration", link: "/design/multi-bank-orchestration/" },
    { label: "Memory portability", link: "/design/memory-portability/" },
    { label: "MCP server", link: "/design/mcp-server/" },
    { label: "Memory lifecycle", link: "/design/memory-lifecycle/" },
    { label: "Access control", link: "/design/access-control/" },
    { label: "Event hooks", link: "/design/event-hooks/" },
    { label: "Memory analytics", link: "/design/memory-analytics/" },
    { label: "Evaluation", link: "/design/evaluation/" },
    { label: "Data governance", link: "/design/data-governance/" },
  ],
  tutorials: [
    { label: "100 Agents in 100 Days", link: "/tutorials/100-agents-in-100-days/" },
  ],
};

export default defineConfig({
  site,
  base,
  integrations: [
    starlight({
      title: "Astrocytes",
      description:
        "Open-source memory framework between agents and storage: retain/recall/synthesize, pluggable backends, governance and observability.",
      plugins: [
        starlightSidebarTopics(
          [
            {
              id: "end-user",
              label: "End User",
              link: "/introduction/",
              icon: "open-book",
              items: topicItems.endUser,
            },
            {
              id: "plugins",
              label: "Plugin Developer",
              link: "/plugins/provider-spi/",
              icon: "puzzle",
              items: topicItems.plugins,
            },
            {
              id: "design",
              label: "Design",
              link: "/design/neuroscience-astrocytes/",
              icon: "document",
              items: topicItems.design,
            },
            {
              id: "tutorials",
              label: "Tutorials",
              link: "/tutorials/100-agents-in-100-days/",
              icon: "star",
              items: topicItems.tutorials,
            },
          ],
          {
            topics: {
              "end-user": ["/", "/index"],
            },
          },
        ),
      ],
      social: [
        {
          icon: "github",
          label: "GitHub",
          href: "https://github.com/AstrocyteAI/astrocytes",
        },
      ],
      customCss: ["./src/styles/custom.css"],
    }),
  ],
});
