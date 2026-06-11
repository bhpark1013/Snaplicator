import { useCallback, useEffect, useMemo, useState, type KeyboardEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { Check, Copy, Eye, EyeOff } from 'lucide-react'

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
    db_user?: string | null
    db_password?: string | null
    db_name?: string | null
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
    const [showPassword, setShowPassword] = useState(false)
    const [copied, setCopied] = useState(false)

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
        setShowPassword(false)
        setCopied(false)
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

    const conn = useMemo(() => {
        if (!detail) return null
        const host = (typeof window !== 'undefined' && window.location.hostname) || 'localhost'
        return {
            host,
            port: detail.host_port != null ? String(detail.host_port) : '',
            user: detail.db_user ?? '',
            password: detail.db_password ?? '',
            dbname: detail.db_name ?? '',
        }
    }, [detail])

    const connString = useMemo(() => {
        if (!conn) return ''
        return `postgresql://${conn.user}:${conn.password}@${conn.host}:${conn.port}/${conn.dbname}`
    }, [conn])

    const connStringDisplay = useMemo(() => {
        if (!conn) return ''
        const pw = showPassword ? conn.password : '••••••••'
        return `postgresql://${conn.user}:${pw}@${conn.host}:${conn.port}/${conn.dbname}`
    }, [conn, showPassword])

    const onCopyConn = useCallback(async () => {
        if (!connString) return
        try {
            await navigator.clipboard.writeText(connString)
            setCopied(true)
            setTimeout(() => setCopied(false), 1500)
        } catch {
            /* clipboard unavailable */
        }
    }, [connString])

    return (
        <div className="mx-auto max-w-5xl animate-page-in px-6 pb-20 pt-6">
            <div className="mb-2 flex items-center justify-between gap-4 border-b border-border pb-4">
                <h1 className="text-base font-semibold tracking-tight">Clone Detail</h1>
                <div className="flex items-center gap-2">
                    <Button asChild>
                        <Link to="/">← Back</Link>
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

            {detail && conn && (
                <Card className="mt-4">
                    <CardTitle>Connection</CardTitle>
                    <div className="mt-3 grid gap-px overflow-hidden rounded-md border border-border bg-border text-[13px]">
                        <div className="flex items-center gap-3 bg-card px-3.5 py-2.5">
                            <span className="w-24 flex-none text-muted-foreground">Host</span>
                            <span className="min-w-0 truncate font-mono text-zinc-200">{conn.host}</span>
                        </div>
                        <div className="flex items-center gap-3 bg-card px-3.5 py-2.5">
                            <span className="w-24 flex-none text-muted-foreground">Port</span>
                            <span className="font-mono text-zinc-200">{conn.port || 'N/A'}</span>
                        </div>
                        <div className="flex items-center gap-3 bg-card px-3.5 py-2.5">
                            <span className="w-24 flex-none text-muted-foreground">User</span>
                            <span className="min-w-0 truncate font-mono text-zinc-200">{conn.user || '-'}</span>
                        </div>
                        <div className="flex items-center gap-3 bg-card px-3.5 py-2.5">
                            <span className="w-24 flex-none text-muted-foreground">Password</span>
                            <span className="min-w-0 truncate font-mono text-zinc-200">{showPassword ? (conn.password || '-') : '••••••••'}</span>
                            <button
                                type="button"
                                onClick={() => setShowPassword((v) => !v)}
                                aria-label={showPassword ? 'Hide password' : 'Show password'}
                                className="ml-auto flex-none rounded-md p-1 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            >
                                {showPassword ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                            </button>
                        </div>
                    </div>

                    <div className="mt-3">
                        <div className="mb-1.5 text-xs font-medium text-muted-foreground">Connection string</div>
                        <div className="flex items-center gap-2 rounded-md border border-border bg-secondary px-3 py-2">
                            <code className="min-w-0 flex-1 truncate font-mono text-[12.5px] text-zinc-300">{connStringDisplay}</code>
                            <button
                                type="button"
                                onClick={() => setShowPassword((v) => !v)}
                                aria-label={showPassword ? 'Hide password' : 'Show password'}
                                className="flex-none rounded-md p-1 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            >
                                {showPassword ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                            </button>
                            <button
                                type="button"
                                onClick={onCopyConn}
                                aria-label="Copy connection string"
                                className="flex-none rounded-md p-1 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            >
                                {copied ? <Check className="size-4 text-success" /> : <Copy className="size-4" />}
                            </button>
                        </div>
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
