import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { Check, Copy, Eye, EyeOff, Pencil } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardTitle } from '@/components/ui/card'
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogTitle,
} from '@/components/ui/dialog'
import { Textarea } from '@/components/ui/textarea'
import { copyText } from '@/lib/utils'

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
    const [showPassword, setShowPassword] = useState(false)
    const [copied, setCopied] = useState(false)
    const [refreshOpen, setRefreshOpen] = useState(false)
    const [editOpen, setEditOpen] = useState(false)
    const [editDesc, setEditDesc] = useState('')

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

    useEffect(() => {
        fetchDetail()
        fetchCloneSnapshots()
        fetchAllSnapshots()
    }, [fetchDetail, fetchCloneSnapshots, fetchAllSnapshots])

    useEffect(() => {
        setShowPassword(false)
        setCopied(false)
    }, [cloneId])

    const openRefresh = useCallback(() => {
        if (!detail) return
        if (!detail.container_name) {
            setError('Cannot refresh: the clone has no container.')
            return
        }
        setRefreshOpen(true)
    }, [detail])

    const confirmRefresh = useCallback(async () => {
        if (!detail?.container_name) return
        setActionBusy(true)
        setMessage(null)
        setError(null)
        try {
            const encoded = encodeURIComponent(detail.container_name)
            const r = await fetch(`${base}/clones/${encoded}/refresh`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            setMessage(`Refreshed ${res.refreshed_container}`)
            setRefreshOpen(false)
            await Promise.all([fetchDetail(), fetchCloneSnapshots()])
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setActionBusy(false)
        }
    }, [base, detail, fetchCloneSnapshots, fetchDetail])

    const openEdit = useCallback(() => {
        setEditDesc(detail?.description ?? '')
        setEditOpen(true)
    }, [detail])

    const saveDescription = useCallback(async () => {
        if (!cloneId) return
        setActionBusy(true)
        setMessage(null)
        setError(null)
        try {
            const r = await fetch(`${base}/clones/${encodeURIComponent(cloneId)}/description`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ description: editDesc.trim() }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            setMessage('Description updated.')
            setEditOpen(false)
            await fetchDetail()
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setActionBusy(false)
        }
    }, [base, cloneId, editDesc, fetchDetail])

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
            await Promise.all([fetchDetail(), fetchCloneSnapshots()])
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setActionBusy(false)
        }
    }, [base, cloneId, fetchCloneSnapshots, fetchDetail])

    const updatedAt = useMemo(() => {
        const candidates = [detail?.refreshed_at, detail?.reset_at].filter(Boolean) as string[]
        if (!candidates.length) return null
        return candidates.sort().pop() as string
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
        const ok = await copyText(connString)
        if (!ok) {
            setError('Copy failed. Select the connection string and copy manually.')
            return
        }
        setCopied(true)
        setTimeout(() => setCopied(false), 1500)
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
                    <div className="mt-3 grid gap-4">
                        <div>
                            <div className="mb-1 flex items-center gap-2">
                                <span className="text-xs font-medium text-muted-foreground">Description</span>
                                <button
                                    type="button"
                                    onClick={openEdit}
                                    aria-label="Edit description"
                                    className="rounded p-0.5 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                                >
                                    <Pencil className="size-3.5" />
                                </button>
                            </div>
                            <div className="text-[15px] font-medium text-zinc-100">
                                {detail.description?.trim() ? detail.description : <span className="text-muted-foreground">(no description)</span>}
                            </div>
                        </div>
                        <div className="grid grid-cols-2 gap-4">
                            <div>
                                <div className="mb-1 text-xs font-medium text-muted-foreground">Created</div>
                                <div className="text-[13px] text-zinc-200">{detail.created_at ? new Date(detail.created_at).toLocaleString() : '—'}</div>
                            </div>
                            <div>
                                <div className="mb-1 text-xs font-medium text-muted-foreground">Updated</div>
                                <div className="text-[13px] text-zinc-200">{updatedAt ? new Date(updatedAt).toLocaleString() : 'Never'}</div>
                            </div>
                        </div>
                    </div>

                    <div className="mt-4 flex flex-wrap gap-2">
                        <Button onClick={openRefresh} disabled={actionBusy || !detail.has_container}>Refresh</Button>
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

            <Dialog open={refreshOpen} onOpenChange={(open) => { if (!actionBusy) setRefreshOpen(open) }}>
                <DialogContent>
                    <DialogTitle>Refresh clone</DialogTitle>
                    <DialogDescription>
                        This re-syncs the clone with the latest data from main and recreates its container.
                        The current description is kept, and any changes made inside this clone are discarded.
                    </DialogDescription>
                    <DialogFooter>
                        <Button onClick={() => setRefreshOpen(false)} disabled={actionBusy}>Cancel</Button>
                        <Button variant="primary" onClick={confirmRefresh} disabled={actionBusy}>
                            {actionBusy ? 'Refreshing...' : 'Refresh'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            <Dialog open={editOpen} onOpenChange={(open) => { if (!actionBusy) setEditOpen(open) }}>
                <DialogContent>
                    <DialogTitle>Edit description</DialogTitle>
                    <Textarea
                        autoFocus
                        value={editDesc}
                        onChange={(e) => setEditDesc(e.target.value)}
                        placeholder="Describe this clone"
                        className="min-h-[90px] font-sans text-[13px]"
                    />
                    <DialogFooter>
                        <Button onClick={() => setEditOpen(false)} disabled={actionBusy}>Cancel</Button>
                        <Button variant="primary" onClick={saveDescription} disabled={actionBusy}>
                            {actionBusy ? 'Saving...' : 'Save'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </div>
    )
}
