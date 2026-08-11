// The MCP endpoint is served by the API process at /mcp, so it is reachable
// wherever this page's own API is: through the same proxy, on the same
// origin. Derived rather than written down, because the previous constant
// was a tailnet IP and a port that only one install ever had — every other
// deployment was handed a URL to nowhere.
// Trailing slash on purpose: the mounted app's route is the mount root, and
// asking for it without the slash earns a redirect to it — which, behind the
// /api prefix, points at a path the proxy does not strip and the SPA
// fallback happily answers with index.html.
export const MCP_BASE_URL = `${window.location.origin}/api/mcp/`

// Endpoint scoped so that mutating clone tools (refresh / reset / delete /
// create-snapshot) may only target the given clone — matched by host port.
// See backend/app/mcp_server.py:_assert_clone_mutable.
export function mcpScopedUrl(port: number | string): string {
    return `${MCP_BASE_URL}?clones=${port}`
}

// One fixed server name; the clone is selected purely by the ?clones= parameter.
export const MCP_SERVER_NAME = 'snaplicator'

// One-liner to register this clone's scoped endpoint in Claude Code.
export function claudeMcpCmd(port: number | string): string {
    return `claude mcp add --transport http ${MCP_SERVER_NAME} '${mcpScopedUrl(port)}'`
}

// ~/.codex/config.toml block to register this clone's scoped endpoint in Codex.
export function codexMcpSnippet(port: number | string): string {
    return `[mcp_servers.${MCP_SERVER_NAME}]\nurl = "${mcpScopedUrl(port)}"`
}
