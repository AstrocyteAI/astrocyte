-- Kong plugin schema for Astrocyte memory integration.

local typedefs = require("kong.db.schema.typedefs")

return {
  name = "astrocyte",
  fields = {
    { consumer = typedefs.no_consumer },
    { protocols = typedefs.protocols_http },
    {
      config = {
        type = "record",
        fields = {
          {
            astrocyte_url = {
              type     = "string",
              required = true,
              default  = "http://localhost:8900",
              description = "Base URL of the Astrocyte standalone gateway.",
            },
          },
          {
            api_key = {
              type     = "string",
              required = false,
              description = "API key for Astrocyte gateway authentication (X-Api-Key header).",
            },
          },
          {
            default_bank_id = {
              type     = "string",
              required = false,
              description = "Default memory bank. Overridden by X-Astrocyte-Bank request header.",
            },
          },
          {
            max_recall_results = {
              type     = "integer",
              required = true,
              default  = 5,
              between  = { 1, 50 },
              description = "Maximum number of memory hits to inject into the system prompt.",
            },
          },
          {
            retain_responses = {
              type     = "boolean",
              required = true,
              default  = true,
              description = "Whether to retain LLM assistant responses as new memories.",
            },
          },
          {
            timeout_ms = {
              type     = "integer",
              required = true,
              default  = 5000,
              between  = { 100, 60000 },
              description = "HTTP timeout in milliseconds for Astrocyte gateway calls.",
            },
          },
        },
      },
    },
  },
}
