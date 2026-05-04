import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";
import starlightSidebarTopics from "starlight-sidebar-topics";
import mermaid from "astro-mermaid";

const owner = process.env.GITHUB_REPOSITORY_OWNER || "AstrocyteAI";
const githubRepository = process.env.GITHUB_REPOSITORY;
const repositoryParts = githubRepository?.split("/").filter(Boolean) ?? [];
const repo =
  repositoryParts.length >= 2 ? repositoryParts[1] : repositoryParts[0];
const site = `https://${owner}.github.io`;
const base = repo ? `/${repo}/` : "/";
/** Absolute URL for link previews (WhatsApp, Slack, etc. require og:image). */
const defaultOgImage = new URL("logo.png", `${site}${base}`).href;

/** Explicit sidebar order (filenames without .md → routes). */
const topicItems = {
  endUser: [
    // ── Getting Started ──
    {
      label: "Getting Started",
      items: [
        { label: "Introduction", link: "/introduction/" },
        { label: "How it works", link: "/end-user/how-it-works/" },
        { label: "Quick Start", link: "/end-user/quick-start/" },
        { label: "FAQ", link: "/end-user/faq/" },
      ],
    },
    // ── Reference ──
    {
      label: "Reference",
      items: [
        { label: "Memory API reference", link: "/end-user/memory-api-reference/" },
        { label: "Configuration reference", link: "/end-user/configuration-reference/" },
      ],
    },
    // ── Setup Guides ──
    {
      label: "Setup Guides",
      items: [
        { label: "Authentication setup", link: "/end-user/authentication-setup/" },
        { label: "Storage backend setup", link: "/end-user/storage-backend-setup/" },
        { label: "Access control setup", link: "/end-user/access-control-setup/" },
        { label: "Bank management", link: "/end-user/bank-management/" },
        { label: "Monitoring & observability", link: "/end-user/monitoring-and-observability/" },
      ],
    },
    // ── Deployment ──
    {
      label: "Deployment",
      items: [
        { label: "Production-grade HTTP service", link: "/end-user/production-grade-http-service/" },
        { label: "Poll ingest", link: "/end-user/poll-ingest-gateway/" },
        { label: "Gateway edge & API gateways", link: "/end-user/gateway-edge-and-api-gateways/" },
      ],
    },
  ],
  plugins: [
    // ── Service Provider Interfaces ──
    {
      label: "Service Provider Interfaces",
      items: [
        { label: "Provider SPI", link: "/plugins/provider-spi/" },
        { label: "Multimodal LLM SPI", link: "/plugins/multimodal-llm-spi/" },
        { label: "Outbound transport", link: "/plugins/outbound-transport/" },
      ],
    },
    // ── Developing Plugins ──
    {
      label: "Developing Plugins",
      items: [
        { label: "Ecosystem & packaging", link: "/plugins/ecosystem-and-packaging/" },
        { label: "Agent framework middleware", link: "/plugins/agent-framework-middleware/" },
        { label: "MIP developer guide", link: "/plugins/mip-developer-guide/" },
      ],
    },
    {
      label: "Integrations",
      collapsed: true,
      items: [
        { label: "LangGraph", link: "/plugins/integrations/langgraph/" },
        { label: "CrewAI", link: "/plugins/integrations/crewai/" },
        { label: "Pydantic AI", link: "/plugins/integrations/pydantic-ai/" },
        { label: "OpenAI Agents SDK", link: "/plugins/integrations/openai-agents/" },
        { label: "Claude Agent SDK", link: "/plugins/integrations/claude-agent-sdk/" },
        { label: "Claude Managed Agents", link: "/plugins/integrations/claude-managed-agents/" },
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
  design: [
    // ── Foundations ──
    {
      label: "Foundations",
      items: [
        { label: "Neuroscience & vocabulary", link: "/design/neuroscience-astrocyte/" },
        { label: "Design principles", link: "/design/design-principles/" },
        { label: "Architecture", link: "/design/architecture/" },
        { label: "C4, deployment & domain model", link: "/design/c4-deployment-domain/" },
        { label: "Product roadmap", link: "/design/product-roadmap/" },
        {
          label: "ADRs",
          collapsed: true,
          items: [
            { label: "ADR-001 Deployment models", link: "/design/adr/adr-001-deployment-models/" },
            { label: "ADR-002 Identity model", link: "/design/adr/adr-002-identity-model/" },
            { label: "ADR-003 Config schema", link: "/design/adr/adr-003-config-schema/" },
            { label: "ADR-004 Recall authority", link: "/design/adr/adr-004-recall-authority/" },
            { label: "ADR-005 JWT identity", link: "/design/adr/adr-005-jwt-identity-extends-actor-model/" },
          ],
        },
      ],
    },
    // ── Trust & Governance ──
    {
      label: "Trust & Governance",
      items: [
        { label: "Identity & external policy", link: "/design/identity-and-external-policy/" },
        { label: "Access control", link: "/design/access-control/" },
        { label: "Data governance", link: "/design/data-governance/" },
        { label: "Sandbox awareness & exfiltration", link: "/design/sandbox-awareness-and-exfiltration/" },
        { label: "Policy layer", link: "/design/policy-layer/" },
      ],
    },
    // ── Runtime & Pipeline ──
    {
      label: "Runtime & Pipeline",
      items: [
        { label: "Built-in pipeline", link: "/design/built-in-pipeline/" },
        { label: "Innovations roadmap", link: "/design/innovations/" },
        { label: "Memory Intent Protocol (MIP)", link: "/design/memory-intent-protocol/" },
        { label: "Multi-bank orchestration", link: "/design/multi-bank-orchestration/" },
        { label: "Storage and data planes", link: "/design/storage-and-data-planes/" },
        { label: "Storage adapter packages", link: "/plugins/ecosystem-and-packaging/#22-tier-1-retrieval-providers" },
      ],
    },
    // ── Integrations & Extensibility ──
    {
      label: "Integrations & Extensibility",
      items: [
        { label: "MCP server", link: "/design/mcp-server/" },
        { label: "Presentation layer & multimodal", link: "/design/presentation-layer-and-multimodal-services/" },
        { label: "Event hooks", link: "/design/event-hooks/" },
        { label: "Memory export sink", link: "/design/memory-export-sink/" },
        { label: "CocoIndex integration roadmap", link: "/design/cocoindex-integration/" },
        { label: "Use-case profiles", link: "/design/use-case-profiles/" },
      ],
    },
    // ── Durability & Lifecycle ──
    {
      label: "Durability & Lifecycle",
      items: [
        { label: "Memory lifecycle", link: "/design/memory-lifecycle/" },
        { label: "Memory portability", link: "/design/memory-portability/" },
      ],
    },
    // ── Quality & Observability ──
    {
      label: "Quality & Observability",
      items: [
        { label: "Bank health & utilization", link: "/design/memory-analytics/" },
        { label: "Evaluation", link: "/design/evaluation/" },
        { label: "Eval Dashboard", link: "/eval/dashboard/" },
      ],
    },
    // ── Meta ──
    {
      label: "Meta",
      collapsed: true,
      items: [
        { label: "Fellowship curriculum mapping", link: "/design/curriculum-mapping/" },
        { label: "Implementation language strategy", link: "/design/implementation-language-strategy/" },
      ],
    },
  ],
  tutorials: [
    // ── Getting Started ──
    {
      label: "Getting Started",
      items: [
        { label: "100 Agents in 100 Days", link: "/tutorials/100-agents-in-100-days/" },
      ],
    },
    // ── Consumer Agents ──
    {
      label: "Consumer Agents",
      collapsed: true,
      items: [
        { label: "Productivity & communication", badge: "1–9", link: "/tutorials/100-agents-in-100-days/#productivity--communication" },
        { label: "Money & subscriptions", badge: "10–14", link: "/tutorials/100-agents-in-100-days/#money-shopping-subscriptions" },
        { label: "Home & logistics", badge: "15–20", link: "/tutorials/100-agents-in-100-days/#home-mobility-logistics" },
        { label: "Food & wellness", badge: "21–26", link: "/tutorials/100-agents-in-100-days/#food-health-adjacent-wellness" },
        { label: "Learning & career", badge: "27–34", link: "/tutorials/100-agents-in-100-days/#learning-career-creativity" },
        { label: "Family & social", badge: "35–40", link: "/tutorials/100-agents-in-100-days/#family-social-local-life" },
        { label: "Hobbies", badge: "41–45", link: "/tutorials/100-agents-in-100-days/#hobbies--specialized-leisure" },
        { label: "Life admin", badge: "46–49", link: "/tutorials/100-agents-in-100-days/#accessibility--life-admin" },
      ],
    },
    // ── Enterprise Agents ──
    {
      label: "Enterprise Agents",
      collapsed: true,
      items: [
        { label: "Support & sales", badge: "50–55", link: "/tutorials/100-agents-in-100-days/#customer-facing-support-success-sales" },
        { label: "IT & workplace", badge: "56–61", link: "/tutorials/100-agents-in-100-days/#internal-it-workplace-hr" },
        { label: "Engineering & platform", badge: "62–68", link: "/tutorials/100-agents-in-100-days/#engineering-quality-platform" },
        { label: "Security & compliance", badge: "69–74", link: "/tutorials/100-agents-in-100-days/#security-risk-compliance" },
        { label: "Finance & procurement", badge: "75–77", link: "/tutorials/100-agents-in-100-days/#finance--procurement" },
        { label: "Legal operations", badge: "78–96", link: "/tutorials/100-agents-in-100-days/#legal-department--legal-operations" },
        { label: "Operations & supply chain", badge: "97–102", link: "/tutorials/100-agents-in-100-days/#operations-supply-chain-field" },
        { label: "Data & product", badge: "103–107", link: "/tutorials/100-agents-in-100-days/#data-analytics-product" },
        { label: "Industry-specific", badge: "108–112", link: "/tutorials/100-agents-in-100-days/#industry-flavored" },
        { label: "Marketing & comms", badge: "113–116", link: "/tutorials/100-agents-in-100-days/#marketing-comms-design-systems" },
        { label: "Strategy & leadership", badge: "117–120", link: "/tutorials/100-agents-in-100-days/#strategy--leadership" },
      ],
    },
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
