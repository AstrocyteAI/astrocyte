import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";
import starlightSidebarTopics from "starlight-sidebar-topics";
import mermaid from "astro-mermaid";

const owner = process.env.GITHUB_REPOSITORY_OWNER || "AstrocyteAI";
const repo = process.env.GITHUB_REPOSITORY?.split("/")?.[1];
const site = `https://${owner}.github.io`;
const base = repo ? `/${repo}/` : "/";
/** Absolute URL for link previews (WhatsApp, Slack, etc. require og:image). */
const defaultOgImage = `${site}${base}logo.png`;

/** Explicit sidebar order (filenames without .md → routes). */
const topicItems = {
  endUser: [
    { label: "Introduction", link: "/introduction/" },
    { label: "Quick Start", link: "/end-user/quick-start/" },
    { label: "Production-grade reference server", link: "/end-user/production-grade-http-service/" },
    { label: "Poll ingest (gateway)", link: "/end-user/poll-ingest-gateway/" },
  ],
  plugins: [
    { label: "Provider SPI", link: "/plugins/provider-spi/" },
    { label: "Ecosystem & packaging", link: "/plugins/ecosystem-and-packaging/" },
    { label: "Outbound transport", link: "/plugins/outbound-transport/" },
    { label: "Multimodal LLM SPI", link: "/plugins/multimodal-llm-spi/" },
    { label: "Agent framework middleware", link: "/plugins/agent-framework-middleware/" },
    {
      label: "Integrations",
      collapsed: true,
      items: [
        { label: "LangGraph", link: "/plugins/integrations/langgraph/" },
        { label: "CrewAI", link: "/plugins/integrations/crewai/" },
        { label: "Pydantic AI", link: "/plugins/integrations/pydantic-ai/" },
        { label: "OpenAI Agents SDK", link: "/plugins/integrations/openai-agents/" },
        { label: "Claude Agent SDK", link: "/plugins/integrations/claude-agent-sdk/" },
        { label: "Google ADK", link: "/plugins/integrations/google-adk/" },
        { label: "AutoGen / AG2", link: "/plugins/integrations/autogen/" },
        { label: "Smolagents", link: "/plugins/integrations/smolagents/" },
        { label: "LlamaIndex", link: "/plugins/integrations/llamaindex/" },
        { label: "Strands Agents", link: "/plugins/integrations/strands/" },
        { label: "Semantic Kernel", link: "/plugins/integrations/semantic-kernel/" },
        { label: "DSPy", link: "/plugins/integrations/dspy/" },
        { label: "CAMEL-AI", link: "/plugins/integrations/camel-ai/" },
        { label: "BeeAI (IBM)", link: "/plugins/integrations/beeai/" },
        { label: "Microsoft Agent", link: "/plugins/integrations/microsoft-agent/" },
        { label: "LiveKit Agents", link: "/plugins/integrations/livekit/" },
        { label: "Haystack", link: "/plugins/integrations/haystack/" },
        { label: "MCP Server", link: "/plugins/integrations/mcp/" },
      ],
    },
  ],
  /** Order matches README §3 bands (Foundations → Trust → Runtime → Durability → Quality). */
  design: [
    { label: "Neuroscience & vocabulary", link: "/design/neuroscience-astrocyte/" },
    { label: "Design principles", link: "/design/design-principles/" },
    { label: "Architecture framework", link: "/design/architecture-framework/" },
    { label: "Architecture brief (C4 & domain)", link: "/design/architecture-brief/" },
    { label: "Product roadmap v1", link: "/design/product-roadmap-v1/" },
    {
      label: "ADRs",
      collapsed: true,
      items: [
        { label: "ADR-001 Deployment models", link: "/design/adr/adr-001-deployment-models/" },
        { label: "ADR-002 Identity model", link: "/design/adr/adr-002-identity-model/" },
        { label: "ADR-003 Config schema", link: "/design/adr/adr-003-config-schema/" },
        { label: "ADR-004 Recall authority", link: "/design/adr/adr-004-recall-authority/" },
      ],
    },
    { label: "Storage and data planes", link: "/design/storage-and-data-planes/" },
    { label: "Fellowship curriculum mapping", link: "/design/curriculum-mapping/" },
    { label: "Implementation language strategy", link: "/design/implementation-language-strategy/" },
    { label: "Identity & external policy", link: "/design/identity-and-external-policy/" },
    { label: "Access control", link: "/design/access-control/" },
    { label: "Sandbox awareness & exfiltration", link: "/design/sandbox-awareness-and-exfiltration/" },
    { label: "Data governance", link: "/design/data-governance/" },
    { label: "Innovations roadmap", link: "/design/innovations/" },
    {
      label: "Presentation layer & multimodal services",
      link: "/design/presentation-layer-and-multimodal-services/",
    },
    { label: "Policy layer", link: "/design/policy-layer/" },
    { label: "Use-case profiles", link: "/design/use-case-profiles/" },
    { label: "Built-in pipeline", link: "/design/built-in-pipeline/" },
    { label: "Multi-bank orchestration", link: "/design/multi-bank-orchestration/" },
    { label: "MCP server", link: "/design/mcp-server/" },
    { label: "Memory Intent Protocol (MIP)", link: "/design/memory-intent-protocol/" },
    { label: "Memory portability", link: "/design/memory-portability/" },
    { label: "Memory lifecycle", link: "/design/memory-lifecycle/" },
    { label: "Event hooks", link: "/design/event-hooks/" },
    { label: "Memory export sink", link: "/design/memory-export-sink/" },
    { label: "Bank health & utilization", link: "/design/memory-analytics/" },
    { label: "Evaluation", link: "/design/evaluation/" },
    { label: "Eval Dashboard", link: "/eval/dashboard/" },
  ],
  tutorials: [
    { label: "100 Agents in 100 Days", link: "/tutorials/100-agents-in-100-days/" },
  ],
};

export default defineConfig({
  site,
  base,
  integrations: [
    // Must run before Starlight so ```mermaid blocks are transformed for MD content.
    mermaid({ autoTheme: true }),
    starlight({
      components: {
        Header: "./src/components/Header.astro",
        ThemeSelect: "./src/components/ThemeSelect.astro",
        Hero: "./src/components/Hero.astro",
      },
      title: "Astrocyte",
      logo: {
        light: "./public/logo.svg",
        dark: "./public/logo-dark.svg",
        alt: "Astrocyte — memory framework for AI agents",
      },
      favicon: "/favicon.svg",
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
              link: "/design/neuroscience-astrocyte/",
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
          href: "https://github.com/AstrocyteAI/astrocyte",
        },
      ],
      head: [
        {
          tag: "meta",
          attrs: {
            property: "og:image",
            content: defaultOgImage,
          },
        },
        {
          tag: "meta",
          attrs: {
            property: "og:image:type",
            content: "image/png",
          },
        },
        {
          tag: "meta",
          attrs: {
            property: "og:image:alt",
            content: "Astrocyte — memory framework for AI agents",
          },
        },
        {
          tag: "meta",
          attrs: {
            name: "twitter:image",
            content: defaultOgImage,
          },
        },
      ],
      customCss: ["./src/styles/custom.css"],
    }),
  ],
});
