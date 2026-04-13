-- Astrocyte memory plugin for Apache APISIX.
--
-- Intercepts LLM API calls (OpenAI-compatible /chat/completions) and:
--   Pre-hook  (access phase):  recalls memories → injects into system prompt
--   Post-hook (body_filter):   retains assistant response as new memory
--
-- Requires a running Astrocyte standalone gateway (astrocyte-gateway-py).
--
-- Install:
--   1. Copy this file to /usr/local/apisix/apisix/plugins/astrocyte.lua
--   2. Add "astrocyte" to the plugins list in apisix.yaml
--   3. Configure via APISIX Admin API or declarative config

local core    = require("apisix.core")
local http    = require("resty.http")
local cjson   = require("cjson.safe")

local plugin_name = "astrocyte"

local schema = {
  type = "object",
  properties = {
    astrocyte_url = {
      type        = "string",
      default     = "http://localhost:8900",
      description = "Base URL of the Astrocyte standalone gateway.",
    },
    api_key = {
      type        = "string",
      default     = "",
      description = "API key for Astrocyte gateway authentication (X-Api-Key header).",
    },
    default_bank_id = {
      type        = "string",
      default     = "",
      description = "Default memory bank. Overridden by X-Astrocyte-Bank request header.",
    },
    max_recall_results = {
      type        = "integer",
      default     = 5,
      minimum     = 1,
      maximum     = 50,
      description = "Maximum number of memory hits to inject into the system prompt.",
    },
    retain_responses = {
      type        = "boolean",
      default     = true,
      description = "Whether to retain LLM assistant responses as new memories.",
    },
    timeout_ms = {
      type        = "integer",
      default     = 5000,
      minimum     = 100,
      description = "HTTP timeout in milliseconds for Astrocyte gateway calls.",
    },
  },
  required = { "astrocyte_url" },
}

local _M = {
  version  = 0.1,
  priority = 800,
  name     = plugin_name,
  schema   = schema,
}

-- ---------------------------------------------------------------------------
-- Helpers
-- ---------------------------------------------------------------------------

--- POST JSON to an Astrocyte gateway endpoint and return decoded body.
local function astrocyte_request(conf, path, payload)
  local httpc = http.new()
  httpc:set_timeout(conf.timeout_ms)

  local url = conf.astrocyte_url .. path
  local body = cjson.encode(payload)

  local res, err = httpc:request_uri(url, {
    method  = "POST",
    body    = body,
    headers = {
      ["Content-Type"] = "application/json",
      ["X-Api-Key"]    = conf.api_key or "",
    },
  })

  if not res then
    core.log.warn("[astrocyte] request to ", path, " failed: ", err)
    return nil, err
  end

  if res.status ~= 200 then
    core.log.warn("[astrocyte] ", path, " returned HTTP ", res.status)
    return nil, "HTTP " .. res.status
  end

  local decoded, decode_err = cjson.decode(res.body)
  if not decoded then
    core.log.warn("[astrocyte] JSON decode error: ", decode_err)
    return nil, decode_err
  end

  return decoded
end

--- Extract the last user message from an OpenAI-compatible messages array.
local function extract_last_user_message(messages)
  if not messages then return nil end
  for i = #messages, 1, -1 do
    if messages[i].role == "user" then
      return messages[i].content
    end
  end
  return nil
end

--- Format recall hits as a text block for system prompt injection.
local function format_recall_hits(hits)
  if not hits or #hits == 0 then return nil end
  local parts = { "The following memories are relevant to this conversation:\n" }
  for i, hit in ipairs(hits) do
    parts[#parts + 1] = string.format("[Memory %d] %s", i, hit.text or "")
  end
  return table.concat(parts, "\n")
end

-- ---------------------------------------------------------------------------
-- Plugin phases
-- ---------------------------------------------------------------------------

function _M.access(conf, ctx)
  -- Read and parse the request body
  local raw_body = core.request.get_body()
  if not raw_body or raw_body == "" then return end

  local body, err = cjson.decode(raw_body)
  if not body or not body.messages then return end

  -- Extract the user query
  local query = extract_last_user_message(body.messages)
  if not query then return end

  -- Resolve bank_id from header or config default
  local bank_id = core.request.header(ctx, "X-Astrocyte-Bank")
  if (not bank_id or bank_id == "") and conf.default_bank_id ~= "" then
    bank_id = conf.default_bank_id
  end
  if not bank_id or bank_id == "" then
    core.log.debug("[astrocyte] no bank_id resolved; skipping recall")
    return
  end

  -- Call Astrocyte recall
  local recall_result = astrocyte_request(conf, "/v1/recall", {
    query       = query,
    bank_id     = bank_id,
    max_results = conf.max_recall_results,
  })

  if not recall_result or not recall_result.hits or #recall_result.hits == 0 then
    return
  end

  -- Inject memories into the system prompt
  local memory_block = format_recall_hits(recall_result.hits)
  if not memory_block then return end

  local has_system = false
  for _, msg in ipairs(body.messages) do
    if msg.role == "system" then
      msg.content = msg.content .. "\n\n" .. memory_block
      has_system = true
      break
    end
  end

  if not has_system then
    table.insert(body.messages, 1, {
      role    = "system",
      content = memory_block,
    })
  end

  -- Replace request body
  local new_body = cjson.encode(body)
  ngx.req.set_body_data(new_body)
  core.request.set_header(ctx, "Content-Length", #new_body)

  -- Stash bank_id for the response phase
  ctx.astrocyte_bank_id = bank_id
end

function _M.body_filter(conf, ctx)
  if not conf.retain_responses then return end

  local bank_id = ctx.astrocyte_bank_id
  if not bank_id then return end

  -- Accumulate response body chunks
  local chunk = ngx.arg[1]
  local eof   = ngx.arg[2]

  local buf = ctx.astrocyte_response_buffer or ""
  buf = buf .. (chunk or "")
  ctx.astrocyte_response_buffer = buf

  if not eof then return end

  -- Parse the complete response
  local body = cjson.decode(buf)
  if not body then return end

  -- Extract assistant message (OpenAI format)
  local assistant_text = nil
  if body.choices and body.choices[1] and body.choices[1].message then
    assistant_text = body.choices[1].message.content
  end

  if not assistant_text or assistant_text == "" then return end

  -- Fire-and-forget retain
  local ok, retain_err = pcall(function()
    astrocyte_request(conf, "/v1/retain", {
      content = assistant_text,
      bank_id = bank_id,
      tags    = { "llm_response", "apisix" },
    })
  end)

  if not ok then
    core.log.warn("[astrocyte] retain failed: ", retain_err)
  end
end

return _M
