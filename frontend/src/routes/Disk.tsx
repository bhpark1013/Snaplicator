import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { cloneLabel } from '@/lib/cloneLabel'
import { cn, formatBytes } from '@/lib/utils'

// GET /usage/disk — block ownership across subvolumes, from the backend's
// background FIEMAP pass (services/divergence.py).
interface DiskItem {
    name: string
    kind: 'live' | 'clone' | 'snapshot'
    created_at: string | null
    exclusive_bytes: number
    not_in_live_bytes: number
    referenced_bytes: number
}

interface DiskGroup {
    names: string[]
    bytes: number
}

interface DiskResponse {
    measured_at: string | null
    duration_seconds: number | null
    live: string
    live_bytes: number | null
    referenced_bytes: number | null
    not_in_live_bytes: number | null
    fs_size_bytes: number | null
    fs_used_bytes: number | null
    items: DiskItem[]
    groups: DiskGroup[]
    refreshing: boolean
    error: string | null
}

interface Named {
    name: string
    display_name?: string | null
    description?: string | null
}

const HATCH = {
    backgroundImage:
        'repeating-linear-gradient(135deg, rgb(var(--warning) / 0.55) 0 3px, rgb(var(--warning) / 0.15) 3px 6px)',
}

function Swatch({ className, style }: { className?: string; style?: React.CSSProperties }) {
    return <span className={cn('inline-block size-2.5 flex-none rounded-[2px]', className)} style={style} />
}

function pct(n: number | null | undefined, of: number | null | undefined): string {
    if (!n || !of) return '0%'
    return `${Math.max(0, Math.min(100, (n / of) * 100))}%`
}

export function Disk() {
    const api = import.meta.env.VITE_API_BASE_URL || ''
    const base = api ? api : '/api'

    const [data, setData] = useState<DiskResponse | null>(null)
    const [error, setError] = useState<string | null>(null)
    const [labels, setLabels] = useState<Record<string, string>>({})
    const [selected, setSelected] = useState<Set<string>>(new Set())

    const load = useCallback(() => {
        fetch(`${base}/usage/disk`)
            .then((r) => (r.ok ? r.json() : Promise.reject(r)))
            .then((d: DiskResponse) => { setData(d); setError(null) })
            .catch(async (e) => setError(e?.status ? `${e.status} ${await e.text()}` : String(e)))
    }, [base])

    useEffect(() => {
        load()
        // Names people gave their clones and snapshots (best effort).
        Promise.all([
            fetch(`${base}/clones`).then((r) => (r.ok ? r.json() : [])).catch(() => []),
            fetch(`${base}/snapshots`).then((r) => (r.ok ? r.json() : [])).catch(() => []),
        ]).then(([clones, snaps]: [Named[], Named[]]) => {
            const m: Record<string, string> = {}
            for (const c of Array.isArray(clones) ? clones : []) m[c.name] = cloneLabel(c)
            for (const s of Array.isArray(snaps) ? snaps : []) if (s.description?.trim()) m[s.name] = s.description.trim()
            setLabels(m)
        })
    }, [base, load])

    useEffect(() => {
        if (!data?.refreshing) return
        const t = setInterval(load, 5000)
        return () => clearInterval(t)
    }, [data?.refreshing, load])

    const refresh = () => {
        fetch(`${base}/usage/disk/refresh`, { method: 'POST' }).finally(load)
    }

    const rows = useMemo(() => (data?.items ?? []).filter((i) => i.kind !== 'live'), [data])
    const maxDiverged = useMemo(() => Math.max(1, ...rows.map((r) => r.not_in_live_bytes)), [rows])

    // Deleting a set frees every block held only by members of the set —
    // including what they share with each other, which no single row shows.
    const freed = useMemo(() => {
        if (!data || selected.size === 0) return 0
        let sum = 0
        for (const g of data.groups) if (g.names.every((n) => selected.has(n))) sum += g.bytes
        return sum
    }, [data, selected])
    const selectedExclusiveSum = useMemo(
        () => rows.filter((r) => selected.has(r.name)).reduce((a, r) => a + r.exclusive_bytes, 0),
        [rows, selected],
    )

    const toggle = (name: string) =>
        setSelected((prev) => {
            const next = new Set(prev)
            if (next.has(name)) next.delete(name)
            else next.add(name)
            return next
        })

    const fsSize = data?.fs_size_bytes ?? null
    const fsUsed = data?.fs_used_bytes ?? null
    const other = fsUsed != null && data?.referenced_bytes != null ? Math.max(0, fsUsed - data.referenced_bytes) : null

    return (
        <div className="mx-auto max-w-6xl animate-page-in px-6 pb-28 pt-6">
            <div className="mb-2 flex items-center justify-between gap-4 border-b border-border pb-4">
                <h1 className="text-base font-semibold tracking-tight">Disk</h1>
                <div className="flex items-center gap-3 text-[11px] text-muted-foreground">
                    {data?.refreshing ? (
                        <span className="flex items-center gap-1.5">
                            <span className="size-1.5 animate-pulse rounded-full bg-warning" /> Measuring…
                        </span>
                    ) : data?.measured_at ? (
                        <span title={data.duration_seconds != null ? `took ${data.duration_seconds}s` : undefined}>
                            Measured {new Date(data.measured_at).toLocaleString()}
                        </span>
                    ) : null}
                    <Button onClick={refresh} disabled={!!data?.refreshing}>Measure now</Button>
                </div>
            </div>

            {error && <p className="mt-3 text-[13px] text-destructive">{error}</p>}
            {data?.error && <p className="mt-3 text-[13px] text-destructive">Last measurement failed: {data.error}</p>}

            {/* pool */}
            <Card className="mt-4">
                <div className="mb-2 flex items-baseline justify-between text-[13px]">
                    <span className="font-medium">Pool</span>
                    <span className="text-muted-foreground">
                        {formatBytes(fsUsed)} used of {formatBytes(fsSize)}
                    </span>
                </div>
                <div className="flex h-3 overflow-hidden rounded bg-secondary">
                    <div className="bg-primary" style={{ width: pct(data?.live_bytes, fsSize) }} />
                    <div className="bg-warning/80" style={{ width: pct(data?.not_in_live_bytes, fsSize) }} />
                    <div className="bg-zinc-500" style={{ width: pct(other, fsSize) }} />
                </div>
                <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-[12px] text-muted-foreground">
                    <span className="flex items-center gap-1.5"><Swatch className="bg-primary" />Live replica <b className="font-medium text-foreground">{formatBytes(data?.live_bytes)}</b></span>
                    <span className="flex items-center gap-1.5"><Swatch className="bg-warning/80" />Only in clones &amp; snapshots <b className="font-medium text-foreground">{formatBytes(data?.not_in_live_bytes)}</b></span>
                    <span className="flex items-center gap-1.5" title="btrfs metadata, and blocks of partly-overwritten extents that btrfs keeps until the whole extent is free">
                        <Swatch className="bg-zinc-500" />Metadata &amp; partly-used extents <b className="font-medium text-foreground">{formatBytes(other)}</b>
                    </span>
                </div>
            </Card>

            {/* per subvolume */}
            <Card className="mt-4 p-0">
                <div className="flex flex-wrap items-center gap-x-5 gap-y-1 border-b border-border px-4 py-2.5 text-[12px] text-muted-foreground">
                    <span className="flex items-center gap-1.5"><Swatch className="bg-warning" />Only this one — freed if deleted</span>
                    <span className="flex items-center gap-1.5"><Swatch style={HATCH} />Shared with other clones/snapshots</span>
                    <span className="ml-auto">bar = not in live replica</span>
                </div>
                {rows.length === 0 ? (
                    <p className="px-4 py-6 text-[13px] text-muted-foreground">
                        {data?.refreshing ? 'First measurement running…' : 'No measurement yet.'}
                    </p>
                ) : (
                    <table className="w-full text-[13px]">
                        <thead>
                            <tr className="text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                                <th className="w-8 px-4 py-2" />
                                <th className="py-2 pr-3 font-semibold">Name</th>
                                <th className="py-2 pr-3 font-semibold">Created</th>
                                <th className="w-[34%] py-2 pr-3 font-semibold" />
                                <th className="py-2 pr-3 text-right font-semibold">Only this</th>
                                <th className="py-2 pr-4 text-right font-semibold">Not in live</th>
                            </tr>
                        </thead>
                        <tbody>
                            {rows.map((r) => {
                                const shared = Math.max(0, r.not_in_live_bytes - r.exclusive_bytes)
                                const label = labels[r.name]
                                const on = selected.has(r.name)
                                return (
                                    <tr
                                        key={r.name}
                                        className={cn('border-t border-border/60 hover:bg-secondary/40', on && 'bg-secondary/60')}
                                        onClick={() => toggle(r.name)}
                                    >
                                        <td className="px-4 py-2">
                                            <input type="checkbox" checked={on} readOnly className="accent-[rgb(var(--warning))]" aria-label={`select ${r.name}`} />
                                        </td>
                                        <td className="max-w-[300px] py-2 pr-3">
                                            <div className="flex items-center gap-2">
                                                <span className={cn(
                                                    'flex-none rounded px-1.5 py-px text-[10px] font-medium uppercase',
                                                    r.kind === 'snapshot' ? 'bg-sky-500/15 text-sky-300' : 'bg-emerald-500/15 text-emerald-300',
                                                )}>{r.kind}</span>
                                                {r.kind === 'clone' ? (
                                                    <Link
                                                        to={`/clones/${encodeURIComponent(r.name)}`}
                                                        onClick={(e) => e.stopPropagation()}
                                                        className="truncate hover:underline"
                                                        title={r.name}
                                                    >{label || r.name}</Link>
                                                ) : (
                                                    <span className="truncate" title={r.name}>{label || r.name}</span>
                                                )}
                                            </div>
                                        </td>
                                        <td className="whitespace-nowrap py-2 pr-3 text-muted-foreground">
                                            {r.created_at ? new Date(r.created_at).toLocaleDateString() : ''}
                                        </td>
                                        <td className="py-2 pr-3">
                                            <div className="flex h-2.5 overflow-hidden rounded-sm bg-secondary">
                                                <div className="bg-warning" style={{ width: pct(r.exclusive_bytes, maxDiverged) }} />
                                                <div style={{ ...HATCH, width: pct(shared, maxDiverged) }} />
                                            </div>
                                        </td>
                                        <td className="whitespace-nowrap py-2 pr-3 text-right tabular-nums">{formatBytes(r.exclusive_bytes)}</td>
                                        <td className="whitespace-nowrap py-2 pr-4 text-right tabular-nums text-muted-foreground">{formatBytes(r.not_in_live_bytes)}</td>
                                    </tr>
                                )
                            })}
                        </tbody>
                    </table>
                )}
            </Card>

            {/* selection */}
            {selected.size > 0 && (
                <div className="fixed inset-x-0 bottom-4 z-20 flex justify-center px-4">
                    <div className="flex items-center gap-4 rounded-lg border border-border bg-card px-4 py-2.5 text-[13px] shadow-lg">
                        <span className="text-muted-foreground">{selected.size} selected</span>
                        <span>
                            Deleting frees <b className="font-semibold text-warning">~{formatBytes(freed)}</b>
                        </span>
                        {freed > selectedExclusiveSum && (
                            <span className="text-[12px] text-muted-foreground" title="Blocks they share with each other are freed only when all of them go">
                                ({formatBytes(selectedExclusiveSum)} alone + {formatBytes(freed - selectedExclusiveSum)} shared among them)
                            </span>
                        )}
                        <Button onClick={() => setSelected(new Set())}>Clear</Button>
                    </div>
                </div>
            )}
        </div>
    )
}
