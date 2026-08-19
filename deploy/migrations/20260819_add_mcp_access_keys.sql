-- Access keys for the read-only MCP endpoint.
-- Each key works either as a URL path segment (/mcp/<secret>) or as a bearer
-- token, so clients that cannot send custom headers are still supported.
-- Deliberately NOT in the MCP query allow-list, so execute_sql cannot read it.

CREATE TABLE IF NOT EXISTS public.mcp_access_keys (
    key_id       bigserial PRIMARY KEY,
    label        text NOT NULL,
    secret       text NOT NULL UNIQUE,
    enabled      boolean NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    use_count    bigint NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_mcp_access_keys_enabled
    ON public.mcp_access_keys (secret)
    WHERE enabled;
