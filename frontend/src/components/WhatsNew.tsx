import { useState } from 'react'
import { Check, ChevronDown, Copy, GitBranch, Sparkles, Terminal, X } from 'lucide-react'

import { cn, copyText } from '@/lib/utils'

// Bump this when there's something new worth re-surfacing; a higher version
// re-shows the panel even for users who dismissed an older one.
const VERSION = 2
const STORAGE_KEY = 'snaplicator.whatsnew.dismissedVersion'

// Tailnet-only MCP endpoint (streamable HTTP) exposed by the prod server.
const MCP_URL = 'http://100.93.143.119:8765/mcp'
const CLAUDE_CMD = `claude mcp add --transport http snaplicator ${MCP_URL}`
const CODEX_CMD = `[mcp_servers.snaplicator]
url = "${MCP_URL}"`

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

export function WhatsNew() {
    const [dismissed, setDismissed] = useState(() => {
        try {
            return Number(localStorage.getItem(STORAGE_KEY) || '0') >= VERSION
        } catch {
            return false
        }
    })
    const [open, setOpen] = useState(true)

    if (dismissed) return null

    const dismiss = () => {
        try {
            localStorage.setItem(STORAGE_KEY, String(VERSION))
        } catch {
            /* ignore */
        }
        setDismissed(true)
    }

    return (
        <div className="mt-4 overflow-hidden rounded-lg border border-primary/30 bg-primary/[0.06]">
            <div className="flex items-center gap-2 px-4 py-2.5">
                <Sparkles className="size-4 flex-none text-[#9aa3ee]" />
                <button
                    type="button"
                    onClick={() => setOpen((v) => !v)}
                    className="flex min-w-0 flex-1 items-center gap-2 text-left focus-visible:outline-none"
                >
                    <span className="text-[13px] font-semibold tracking-tight text-zinc-100">What's new &amp; tips</span>
                    <span className="rounded-full bg-primary/20 px-1.5 py-px text-[10px] font-medium text-[#b9c0ff]">MCP</span>
                    <ChevronDown className={cn('size-4 text-muted-foreground transition-transform', open && 'rotate-180')} />
                </button>
                <button
                    type="button"
                    onClick={dismiss}
                    aria-label="Dismiss"
                    title="Don't show again"
                    className="flex-none rounded-md p-1 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                    <X className="size-4" />
                </button>
            </div>

            {open && (
                <div className="grid gap-4 border-t border-primary/20 px-4 py-3.5">
                    {/* feature highlights */}
                    <div className="grid gap-1.5">
                        <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                            <GitBranch className="size-3.5" /> Recently added
                        </div>
                        <ul className="grid gap-1 text-[13px] text-zinc-300">
                            <li>• <span className="text-zinc-100">Snapshot lineage graph</span> — see how snapshots relate; select a node to act on it or move it (no drag needed).</li>
                            <li>• <span className="text-zinc-100">Insert between / reorder</span> — choose where a new snapshot lands; click a <span className="text-primary">+</span> to splice it onto an edge.</li>
                            <li>• <span className="text-zinc-100">Retention</span> — set how long a snapshot is kept (1 day → forever) when you create it.</li>
                            <li>• <span className="text-zinc-100">Search</span> snapshots by description, with the graph scrolling to the match.</li>
                        </ul>
                    </div>

                    {/* MCP connection how-to */}
                    <div className="grid gap-2">
                        <div className="flex flex-wrap items-center gap-2">
                            <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Connect from your AI agent (MCP)</span>
                            <span className="font-mono text-[11px] text-muted-foreground">{MCP_URL}</span>
                            <CopyButton value={MCP_URL} label="Copy URL" />
                        </div>
                        <p className="text-[12px] text-muted-foreground">
                            Drive Snaplicator (list/create clones &amp; snapshots, manage lineage) straight from Claude Code or Codex.
                            The endpoint is tailnet-only — your machine must be on the Tailscale network.
                        </p>
                        <div className="grid gap-2.5 sm:grid-cols-2">
                            <Snippet
                                title="Claude Code"
                                hint="Run in your terminal, then restart Claude Code."
                                code={CLAUDE_CMD}
                            />
                            <Snippet
                                title="Codex"
                                hint="Add to ~/.codex/config.toml, then restart Codex."
                                code={CODEX_CMD}
                            />
                        </div>
                    </div>
                </div>
            )}
        </div>
    )
}
