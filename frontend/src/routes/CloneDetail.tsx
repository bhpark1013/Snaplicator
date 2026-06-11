import { useCallback, useEffect, useMemo, useState, type KeyboardEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardTitle } from '@/components/ui/card'

interface CloneDetailResponse {
    name: string
    path: string
    is_btrfs: boolean
    has_container: boolean
    container_name?: string | null
    container_status?: string | null
    container_ports?: string | null
    host_port?: number | null
    is_running: boolean
    container_started_at?: string | null
    description?: string | null
    metadata?: Record<string, unknown> | null
    readonly?: boolean
    created_at?: string | null
    refreshed_at?: string | null
    reset_at?: string | null
    reset_from_snapshot?: string | null
}

interface CloneSnapshotItem {
    name: string
    path: string
    readonly: boolean
    description?: string | null
    metadata?: Record<string, unknown> | null
}

interface SnapshotListItem {
    name: string
    path: string
    readonly: boolean
    description?: string | null
}

interface CloneUsageSummary {
    usage_bytes?: number | null
    fs_size_bytes?: number | null
    fs_used_bytes?: number | null
    calculated_at?: string | null
}

export function CloneDetail() {
    const { cloneId = '' } = useParams<{ cloneId: string }>()
    const navigate = useNavigate()

    const api = import.meta.env.VITE_API_BASE_URL || ''
    const base = api ? api : '/api'

    const [detail, setDetail] = useState<CloneDetailResponse | null>(null)
    const [cloneSnapshots, setCloneSnapshots] = useState<CloneSnapshotItem[]>([])
    const [allSnapshots, setAllSnapshots] = useState<SnapshotListItem[]>([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState<string | null>(null)
    const [message, setMessage] = useState<string | null>(null)
    const [actionBusy, setActionBusy] = useState(false)
    const [usage, setUsage] = useState<CloneUsageSummary | null>(null)
    const [overviewExpanded, setOverviewExpanded] = useState(false)

    const toggleOverview = useCallback(() => {
        setOverviewExpanded((prev) => !prev)
    }, [])

    const handleOverviewKeyDown = useCallback((e: KeyboardEvent<HTMLDivElement>) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            toggleOverview()
        }
    }, [toggleOverview])

    const formatBytes = useCallback((n?: number | null) => {
        if (n == null || isNaN(n)) return '-'
        const units = ['B', 'KB', 'MB', 'GB', 'TB']
        let v = n
        let i = 0
        while (v >= 1024 && i < units.length - 1) {
            v /= 1024
            i++
        }
        return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`
    }, [])

    const fetchDetail = useCallback(async () => {
        if (!cloneId) return
        setLoading(true)
        setError(null)
        try {
            const encoded = encodeURIComponent(cloneId)
            const r = await fetch(`${base}/clones/${encoded}`)
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const data: CloneDetailResponse = await r.json()
            setDetail(data)
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setLoading(false)
        }
    }, [base, cloneId])

    const fetchCloneSnapshots = useCallback(async () => {
        if (!cloneId) return
        try {
            const encoded = encodeURIComponent(cloneId)
            const r = await fetch(`${base}/clones/${encoded}/snapshots`)
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const data: CloneSnapshotItem[] = await r.json()
            setCloneSnapshots(data)
        } catch (e: any) {
            setCloneSnapshots([])
            setError((prev) => prev ?? String(e?.message || e))
        }
    }, [base, cloneId])

    const fetchAllSnapshots = useCallback(async () => {
        try {
            const r = await fetch(`${base}/snapshots`)
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const data: SnapshotListItem[] = await r.json()
            setAllSnapshots(data)
        } catch (e: any) {
            setAllSnapshots([])
            setError((prev) => prev ?? String(e?.message || e))
        }
    }, [base])

    const fetchUsage = useCallback(async () => {
        if (!cloneId) return
        try {
            const encoded = encodeURIComponent(cloneId)
            const r = await fetch(`${base}/clones/${encoded}/usage`)
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const data: CloneUsageSummary = await r.json()
            setUsage(data)
        } catch (e: any) {
            setUsage(null)
            setError((prev) => prev ?? String(e?.message || e))
        }
    }, [base, cloneId])

    useEffect(() => {
        fetchDetail()
        fetchCloneSnapshots()
        fetchAllSnapshots()
        fetchUsage()
    }, [fetchDetail, fetchCloneSnapshots, fetchAllSnapshots, fetchUsage])

    useEffect(() => {
        setOverviewExpanded(false)
    }, [cloneId])

    const onRefresh = useCallback(async () => {
        if (!detail) return
        if (!detail.container_name) {
            setError('Cannot refresh: the clone has no container.')
            return
        }
        const input = window.prompt('Enter a new description, or press OK to keep the current one.', detail.description || '')
        if (input === null) return
        const trimmed = input.trim()

        setActionBusy(true)
        setMessage(null)
        setError(null)
        try {
            const encoded = encodeURIComponent(detail.container_name)
            const r = await fetch(`${base}/clones/${encoded}/refresh`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(trimmed ? { description: trimmed } : {}),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            setMessage(`Refreshed ${res.refreshed_container}`)
            await Promise.all([fetchDetail(), fetchCloneSnapshots(), fetchUsage()])
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setActionBusy(false)
        }
    }, [base, detail, fetchCloneSnapshots, fetchDetail, fetchUsage])

    const onDelete = useCallback(async () => {
        if (!detail) return
        if (!window.confirm('Delete this clone? Its container and subvolume will both be removed.')) return
        setActionBusy(true)
        setError(null)
        setMessage(null)
        try {
            const target = detail.container_name || detail.name
            const r = await fetch(`${base}/clones/${encodeURIComponent(target)}`, { method: 'DELETE' })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            navigate('/')
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setActionBusy(false)
        }
    }, [base, detail, navigate])

    const onCreateSnapshot = useCallback(async () => {
        if (!cloneId) return
        let input = window.prompt('Enter a snapshot description (required).', detail?.description || '')
        if (input === null) return
        input = input.trim()
        if (!input) {
            alert('Description is required.')
            return
        }

        setActionBusy(true)
        setError(null)
        setMessage(null)
        try {
            const r = await fetch(`${base}/clones/${encodeURIComponent(cloneId)}/snapshots`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ description: input }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const created = await r.json()
            setMessage(`Snapshot created: ${created.name}`)
            await fetchCloneSnapshots()
            await fetchAllSnapshots()
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setActionBusy(false)
        }
    }, [base, cloneId, fetchCloneSnapshots, fetchAllSnapshots, detail])

    const onReset = useCallback(async (snapshotName: string) => {
        const ok = window.confirm(`Reset this clone to snapshot ${snapshotName}? The container will be recreated.`)
        if (!ok) return

        setActionBusy(true)
        setMessage(null)
        setError(null)
        try {
            const r = await fetch(`${base}/clones/${encodeURIComponent(cloneId)}/reset`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ snapshot_name: snapshotName }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            setMessage(`Reset to snapshot ${res.snapshot_used}`)
            await Promise.all([fetchDetail(), fetchCloneSnapshots(), fetchUsage()])
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setActionBusy(false)
        }
    }, [base, cloneId, fetchCloneSnapshots, fetchDetail, fetchUsage])

    const metadataEntries = useMemo(() => {
        const meta = detail?.metadata
        if (!meta) return [] as Array<[string, unknown]>
        return Object.entries(meta)
    }, [detail])

    return (
        <div className="mx-auto max-w-5xl animate-page-in px-6 pb-20 pt-6">
            <div className="mb-2 flex items-center justify-between gap-4 border-b border-border pb-4">
                <h1 className="text-base font-semibold tracking-tight">Clone Detail</h1>
                <div className="flex items-center gap-2">
                    <Button asChild>
                        <Link to="/">← Back</Link>
                    </Button>
                    <Button onClick={() => { fetchDetail(); fetchCloneSnapshots(); fetchAllSnapshots() }}>
                        Refresh Data
                    </Button>
                </div>
            </div>

            {loading && <p className="mt-4 text-[13px] text-muted-foreground">Loading...</p>}
            {error && <p className="mt-4 text-[13px] text-destructive">{error}</p>}
            {message && <p className="mt-4 text-[13px] text-success">{message}</p>}

            {detail && (
                <Card className="mt-4">
                    <CardTitle>Overview</CardTitle>
                    <div
                        role="button"
                        tabIndex={0}
                        onClick={toggleOverview}
                        onKeyDown={handleOverviewKeyDown}
                        className="mt-2 grid cursor-pointer gap-1.5 rounded-md border border-border bg-secondary p-3 transition-colors hover:border-border-strong"
                    >
                        <div className="text-[15px] font-semibold">{detail.name}</div>
                        <div className="text-[13px] text-zinc-300">{detail.description?.trim() ? detail.description : '(no description)'}</div>
                        <div className="text-[13px] text-zinc-300">Port: {detail.host_port ?? 'N/A'}</div>
                        <div className="text-xs text-muted-foreground">
                            {overviewExpanded ? 'Click to collapse details' : 'Click to expand details'}
                        </div>
                    </div>

                    {overviewExpanded && (
                        <div className="mt-3 grid gap-2 text-[13px]">
                            <div><strong className="font-semibold">Container:</strong> {detail.container_name || 'none'}</div>
                            <div><strong className="font-semibold">Subvolume:</strong> {detail.name}</div>
                            <div><strong className="font-semibold">Path:</strong> {detail.path}</div>
                            <div><strong className="font-semibold">Status:</strong> {detail.container_status || 'unknown'}</div>
                            <div><strong className="font-semibold">Created:</strong> {detail.created_at ? new Date(detail.created_at).toLocaleString() : 'N/A'}</div>
                            <div><strong className="font-semibold">Usage:</strong> {formatBytes(usage?.usage_bytes)}</div>
                            {usage?.calculated_at && (
                                <div><strong className="font-semibold">Measured:</strong> {new Date(usage.calculated_at).toLocaleString()}</div>
                            )}
                            {metadataEntries.length > 0 && (
                                <div>
                                    <h3 className="mb-1 mt-2 text-[13px] font-semibold">Metadata</h3>
                                    <pre className="max-h-52 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-secondary p-3 font-mono text-xs leading-relaxed text-zinc-300">
                                        {JSON.stringify(detail.metadata, null, 2)}
                                    </pre>
                                </div>
                            )}
                        </div>
                    )}

                    <div className="mt-4 flex flex-wrap gap-2">
                        <Button onClick={onRefresh} disabled={actionBusy || !detail.has_container}>Refresh</Button>
                        <Button onClick={onCreateSnapshot} disabled={actionBusy}>Create Snapshot</Button>
                        <Button variant="destructive" onClick={onDelete} disabled={actionBusy}>Delete</Button>
                    </div>
                </Card>
            )}

            <Card className="mt-4">
                <CardTitle>Snapshots from this Clone</CardTitle>
                {cloneSnapshots.length === 0 ? (
                    <p className="mt-2 text-[13px] text-muted-foreground">No snapshots derived from this clone.</p>
                ) : (
                    <ul className="mt-3 grid gap-2">
                        {cloneSnapshots.map((snap) => (
                            <li key={snap.name} className="flex items-center justify-between gap-3 rounded-md border border-border bg-secondary px-3.5 py-2.5 transition-colors hover:border-border-strong hover:bg-accent">
                                <div className="grid min-w-0 gap-1">
                                    <div className="font-medium">{snap.name}</div>
                                    <div className="text-[13px] text-muted-foreground">{snap.description || '(no description)'}</div>
                                </div>
                                <div className="ml-auto flex flex-shrink-0 gap-2">
                                    <Button onClick={() => onReset(snap.name)} disabled={actionBusy}>Reset to this snapshot</Button>
                                </div>
                            </li>
                        ))}
                    </ul>
                )}
            </Card>

            <Card className="mt-4">
                <CardTitle>All Snapshots</CardTitle>
                {allSnapshots.length === 0 ? (
                    <p className="mt-2 text-[13px] text-muted-foreground">No snapshots found.</p>
                ) : (
                    <ul className="mt-3 grid gap-2">
                        {allSnapshots.map((snap) => (
                            <li key={snap.name} className="flex items-center justify-between gap-3 rounded-md border border-border bg-secondary px-3.5 py-2.5 transition-colors hover:border-border-strong hover:bg-accent">
                                <div className="grid min-w-0 gap-1">
                                    <div className="font-medium">{snap.name}</div>
                                    <div className="text-[13px] text-muted-foreground">{snap.description || '(no description)'}</div>
                                </div>
                                <div className="ml-auto flex flex-shrink-0 items-center gap-2">
                                    <Badge variant="neutral">{snap.readonly ? 'readonly' : 'writable'}</Badge>
                                    <Button onClick={() => onReset(snap.name)} disabled={actionBusy}>Reset to this snapshot</Button>
                                </div>
                            </li>
                        ))}
                    </ul>
                )}
            </Card>
        </div>
    )
}
