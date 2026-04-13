-- Astrocyte memory plugin for Kong Gateway.
--
-- Intercepts LLM API calls (OpenAI-compatible /chat/completions) and:
--   Pre-hook  (access phase):  recalls memories → injects into system prompt
--   Post-hook (body_filter):   retains assistant response as new memory
--
-- Requires a running Astrocyte standalone gateway (astrocyte-gateway-py).
--
-- Install:
--   1. Copy handler.lua and schema.lua to /usr/local/share/lua/5.1/kong/plugins/astrocyte/
--   2. Add "astrocyte" to KONG_PLUGINS env var (or kong.conf plugins list)
--   3. Configure via Kong Admin API or declarative config

local http = require("resty.http")
local cjson = require("cjson.safe")

local AstrocyteHandler = {
  PRIORITY = 800,  -- Run before request-transformer (priority 801)
  VERSION = "0.1.0",
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
    kong.log.warn("[astrocyte] request to ", path, " failed: ", err)
    return nil, err
  end

  if res.status ~= 200 then
    kong.log.warn("[astrocyte] ", path, " returned HTTP ", res.status)
    return nil, "HTTP " .. res.status
  end

  local decoded, decode_err = cjson.decode(res.body)
  if not decoded then
    kong.log.warn("[astrocyte] JSON decode error: ", decode_err)
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
-- Kong phases
-- ---------------------------------------------------------------------------

--- Access phase: recall memories and inject into the request body.
function AstrocyteHandler:access(conf)
  -- Read and parse the request body
  kong.service.request.enable_buffering()
  local raw_body = kong.request.get_raw_body()
  if not raw_body or raw_body == "" then return end

  local body, err = cjson.decode(raw_body)
  if not body or not body.messages then return end

  -- Extract the user query
  local query = extract_last_user_message(body.messages)
  if not query then return end

  -- Resolve bank_id from header or config default
  local bank_id = kong.request.get_header("X-Astrocyte-Bank") or conf.default_bank_id
  if not bank_id then
    kong.log.debug("[astrocyte] no bank_id resolved; skipping recall")
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

  -- Prepend or append to existing system message
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

  -- Replace request body with enriched version
  local new_body = cjson.encode(body)
  kong.service.request.set_raw_body(new_body)
  kong.service.request.set_header("Content-Length", #new_body)

  -- Stash bank_id for the response phase
  kong.ctx.plugin.bank_id = bank_id
end

--- Body filter phase: retain the assistant response.
function AstrocyteHandler:body_filter(conf)
  if not conf.retain_responses then return end

  local bank_id = kong.ctx.plugin.bank_id
  if not bank_id then return end

  -- Accumulate response body chunks
  local chunk = ngx.arg[1]
  local eof   = ngx.arg[2]

  local buf = kong.ctx.plugin.response_buffer or ""
  buf = buf .. (chunk or "")
  kong.ctx.plugin.response_buffer = buf

  if not eof then return end

  -- Parse the complete response
  local body, err = cjson.decode(buf)
  if not body then return end

  -- Extract assistant message (OpenAI format)
  local assistant_text = nil
  if body.choices and body.choices[1] and body.choices[1].message then
    assistant_text = body.choices[1].message.content
  end

  if not assistant_text or assistant_text == "" then return end

  -- Fire-and-forget retain (don't block the response)
  local ok, retain_err = pcall(function()
    astrocyte_request(conf, "/v1/retain", {
      content = assistant_text,
      bank_id = bank_id,
      tags    = { "llm_response", "kong" },
    })
  end)

  if not ok then
    kong.log.warn("[astrocyte] retain failed: ", retain_err)
  end
end

return AstrocyteHandler
