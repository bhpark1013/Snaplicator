// Tailnet-only MCP endpoint (streamable HTTP) exposed by the prod Snaplicator server.
export const MCP_BASE_URL = 'http://100.93.143.119:8765/mcp'

// Endpoint scoped so that mutating clone tools (refresh / reset / delete /
// create-snapshot) may only target the given clone — matched by host port.
// See mcp-server/server.py:_assert_clone_mutable.
export function mcpScopedUrl(port: number | string): string {
    return `${MCP_BASE_URL}?clones=${port}`
}

// A per-clone server name so several clones can be registered side by side.
export function mcpServerName(port: number | string): string {
    return `snaplicator-${port}`
}

// One-liner to register this clone's scoped endpoint in Claude Code.
export function claudeMcpCmd(port: number | string): string {
    return `claude mcp add --transport http ${mcpServerName(port)} '${mcpScopedUrl(port)}'`
}

// ~/.codex/config.toml block to register this clone's scoped endpoint in Codex.
export function codexMcpSnippet(port: number | string): string {
    return `[mcp_servers.${mcpServerName(port)}]\nurl = "${mcpScopedUrl(port)}"`
}
