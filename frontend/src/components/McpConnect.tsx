import { useState } from 'react'
import { Check, Copy, Terminal } from 'lucide-react'

import { Card, CardTitle } from '@/components/ui/card'
import { copyText } from '@/lib/utils'
import { claudeMcpCmd, codexMcpSnippet, mcpScopedUrl, mcpServerName } from '@/lib/mcp'

function CopyButton({ value, label }: { value: string; label?: string }) {
    const [copied, setCopied] = useState(false)
    return (
        <button
            type="button"
            onClick={async () => {
                if (await copyText(value)) {
                    setCopied(true)
                    setTimeout(() => setCopied(false), 1500)
                }
            }}
            className="flex flex-none items-center gap-1 rounded-md border border-border-strong bg-secondary px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:border-primary/60 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
            {copied ? <Check className="size-3 text-success" /> : <Copy className="size-3" />}
            {label ?? (copied ? 'Copied' : 'Copy')}
        </button>
    )
}

function Snippet({ title, hint, code }: { title: string; hint: string; code: string }) {
    return (
        <div className="rounded-md border border-border bg-[#0b0c0e] p-3">
            <div className="mb-1.5 flex items-center justify-between gap-2">
                <div className="flex items-center gap-1.5 text-[12px] font-medium text-zinc-200">
                    <Terminal className="size-3.5 text-[#9aa3ee]" />
                    {title}
                </div>
                <CopyButton value={code} />
            </div>
            <p className="mb-2 text-[11px] text-muted-foreground">{hint}</p>
            <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded bg-black/40 px-2.5 py-2 font-mono text-[12px] leading-relaxed text-zinc-300">{code}</pre>
        </div>
    )
}

/**
 * Persistent "connect your AI agent" card for a single clone. Hands the user a
 * ready-to-paste Claude Code / Codex command whose MCP endpoint is scoped to
 * THIS clone's port, so destructive clone tools can only touch this clone.
 */
export function McpConnect({ port, label }: { port: number; label?: string }) {
    const url = mcpScopedUrl(port)
    return (
        <Card className="mt-4">
            <div className="flex flex-wrap items-center gap-2">
                <CardTitle>Connect from your AI agent (MCP)</CardTitle>
                <span className="rounded-full bg-primary/20 px-1.5 py-px text-[10px] font-medium text-[#b9c0ff]">scoped to this clone</span>
            </div>
            <p className="mb-3 mt-1 text-[12px] text-muted-foreground">
                Drive Snaplicator from Claude Code or Codex. Destructive clone actions (refresh / reset / delete)
                are scoped to {label ? <span className="text-zinc-200">“{label}” </span> : null}
                <span className="text-zinc-200">port {port}</span> only. The endpoint is tailnet-only — you must be on the Tailscale network.
            </p>
            <div className="mb-3 flex flex-wrap items-center gap-2">
                <span className="break-all font-mono text-[11px] text-muted-foreground">{url}</span>
                <CopyButton value={url} label="Copy URL" />
            </div>
            <div className="grid gap-2.5 sm:grid-cols-2">
                <Snippet
                    title="Claude Code"
                    hint="Run in your terminal, then restart Claude Code."
                    code={claudeMcpCmd(port)}
                />
                <Snippet
                    title="Codex"
                    hint={`Add to ~/.codex/config.toml as [mcp_servers.${mcpServerName(port)}], then restart Codex.`}
                    code={codexMcpSnippet(port)}
                />
            </div>
        </Card>
    )
}
