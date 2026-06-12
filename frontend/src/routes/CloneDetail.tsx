import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { Check, Copy, Eye, EyeOff, Pencil } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Card, CardTitle } from '@/components/ui/card'
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { useToast } from '@/components/ui/toast'
import { copyText } from '@/lib/utils'
import { RetentionSelect } from '@/components/RetentionSelect'
import { LineageGraph, computeInsertParams, computeMoveUpdates, type Slot, type SnapshotItem } from '@/components/LineageGraph'

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
    display_name?: string | null
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

export function CloneDetail() {
    const { cloneId = '' } = useParams<{ cloneId: string }>()
    const navigate = useNavigate()
    const toast = useToast()

    const api = import.meta.env.VITE_API_BASE_URL || ''
    const base = api ? api : '/api'

    const [detail, setDetail] = useState<CloneDetailResponse | null>(null)
    const [cloneSnapshots, setCloneSnapshots] = useState<CloneSnapshotItem[]>([])
    const [allSnapshots, setAllSnapshots] = useState<SnapshotItem[]>([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState<string | null>(null)
    const [message, setMessage] = useState<string | null>(null)
    const [actionBusy, setActionBusy] = useState(false)
    const [showPassword, setShowPassword] = useState(false)
    const [copied, setCopied] = useState(false)
    const [refreshOpen, setRefreshOpen] = useState(false)
    const [editOpen, setEditOpen] = useState(false)
    const [editName, setEditName] = useState('')
    const [editDesc, setEditDesc] = useState('')
    const [snapshotOpen, setSnapshotOpen] = useState(false)
    const [snapshotDesc, setSnapshotDesc] = useState('')
    const [snapshotSlot, setSnapshotSlot] = useState<Slot | null>(null)
    const [snapshotRetention, setSnapshotRetention] = useState(14)
    const [deleteOpen, setDeleteOpen] = useState(false)
    const [resetFor, setResetFor] = useState<string | null>(null)
    // the currently selected snapshot name — drives the bottom action panel + move slots
    const [nodeActionFor, setNodeActionFor] = useState<string | null>(null)
    const [moveBusy, setMoveBusy] = useState(false)

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
            const data: SnapshotItem[] = await r.json()
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
        const tid = toast.loading('Refreshing clone from main…')
        try {
            const encoded = encodeURIComponent(detail.container_name)
            const r = await fetch(`${base}/clones/${encoded}/refresh`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            toast.update(tid, 'success', `Refreshed ${res.refreshed_container}`)
            setRefreshOpen(false)
            await Promise.all([fetchDetail(), fetchCloneSnapshots()])
        } catch (e: any) {
            toast.update(tid, 'error', `Refresh failed: ${String(e?.message || e)}`)
            setError(String(e?.message || e))
        } finally {
            setActionBusy(false)
        }
    }, [base, detail, fetchCloneSnapshots, fetchDetail, toast])

    const openEdit = useCallback(() => {
        setEditName(detail?.display_name ?? '')
        setEditDesc(detail?.description ?? '')
        setEditOpen(true)
    }, [detail])

    const saveMeta = useCallback(async () => {
        if (!cloneId) return
        if (!editName.trim()) {
            setError('Name is required.')
            return
        }
        setActionBusy(true)
        setMessage(null)
        setError(null)
        const tid = toast.loading('Updating clone…')
        try {
            const r = await fetch(`${base}/clones/${encodeURIComponent(cloneId)}/description`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: editName.trim(), description: editDesc.trim() }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            toast.update(tid, 'success', 'Clone updated')
            setEditOpen(false)
            await fetchDetail()
        } catch (e: any) {
            toast.update(tid, 'error', `Update failed: ${String(e?.message || e)}`)
            setError(String(e?.message || e))
        } finally {
            setActionBusy(false)
        }
    }, [base, cloneId, editName, editDesc, fetchDetail, toast])

    const confirmDeleteClone = useCallback(async () => {
        if (!detail) return
        setActionBusy(true)
        setError(null)
        setMessage(null)
        const tid = toast.loading('Deleting clone…')
        try {
            const target = detail.container_name || detail.name
            const r = await fetch(`${base}/clones/${encodeURIComponent(target)}`, { method: 'DELETE' })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            toast.update(tid, 'success', 'Clone deleted')
            setDeleteOpen(false)
            navigate('/')
        } catch (e: any) {
            toast.update(tid, 'error', `Delete failed: ${String(e?.message || e)}`)
            setError(String(e?.message || e))
        } finally {
            setActionBusy(false)
        }
    }, [base, detail, navigate, toast])

    const openSnapshot = useCallback(() => {
        setSnapshotDesc('')
        setSnapshotRetention(14)
        // default insertion = right after this clone's most recent snapshot
        const latest = cloneSnapshots.length ? cloneSnapshots[cloneSnapshots.length - 1].name : ''
        setSnapshotSlot(latest ? { kind: 'after', parent: latest } : null)
        setSnapshotOpen(true)
    }, [cloneSnapshots])

    const confirmSnapshot = useCallback(async () => {
        if (!cloneId) return
        const desc = snapshotDesc.trim()
        if (!desc) {
            setError('Snapshot description is required.')
            return
        }
        setActionBusy(true)
        setError(null)
        setMessage(null)
        const tid = toast.loading('Creating snapshot…')
        const { previous_snapshot, insert_before } = snapshotSlot
            ? computeInsertParams(snapshotSlot)
            : { previous_snapshot: null, insert_before: null }
        try {
            const r = await fetch(`${base}/clones/${encodeURIComponent(cloneId)}/snapshots`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ description: desc, previous_snapshot, insert_before, retention_days: snapshotRetention }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const created = await r.json()
            toast.update(tid, 'success', `Snapshot created: ${created.name}`)
            setSnapshotOpen(false)
            await fetchCloneSnapshots()
            await fetchAllSnapshots()
        } catch (e: any) {
            toast.update(tid, 'error', `Snapshot failed: ${String(e?.message || e)}`)
            setError(String(e?.message || e))
        } finally {
            setActionBusy(false)
        }
    }, [base, cloneId, snapshotDesc, snapshotSlot, snapshotRetention, fetchCloneSnapshots, fetchAllSnapshots, toast])

    const confirmReset = useCallback(async () => {
        if (!resetFor) return
        const snapshotName = resetFor
        setActionBusy(true)
        setMessage(null)
        setError(null)
        const tid = toast.loading('Resetting clone to snapshot…')
        try {
            const r = await fetch(`${base}/clones/${encodeURIComponent(cloneId)}/reset`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ snapshot_name: snapshotName }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            toast.update(tid, 'success', `Reset to snapshot ${res.snapshot_used}`)
            setResetFor(null)
            await Promise.all([fetchDetail(), fetchCloneSnapshots()])
        } catch (e: any) {
            toast.update(tid, 'error', `Reset failed: ${String(e?.message || e)}`)
            setError(String(e?.message || e))
        } finally {
            setActionBusy(false)
        }
    }, [base, cloneId, resetFor, fetchCloneSnapshots, fetchDetail, toast])

    const onMoveSnapshot = useCallback(async (name: string, slot: Slot) => {
        setNodeActionFor(null)
        const updates = computeMoveUpdates(allSnapshots, name, slot)
        if (!updates.length) return
        setMoveBusy(true)
        const tid = toast.loading('Moving snapshot…')
        try {
            const r = await fetch(`${base}/snapshots/lineage/batch`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ updates }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            const n = res.applied?.length ?? 0
            toast.update(tid, 'success', `Moved (${n} change${n === 1 ? '' : 's'})`)
            await Promise.all([fetchAllSnapshots(), fetchCloneSnapshots()])
        } catch (e: any) {
            toast.update(tid, 'error', `Move failed: ${String(e?.message || e)}`)
        } finally {
            setMoveBusy(false)
        }
    }, [base, allSnapshots, fetchAllSnapshots, fetchCloneSnapshots, toast])

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

    const slotSummary = useMemo(() => {
        const label = (name: string) => allSnapshots.find((s) => s.name === name)?.description?.trim() || name
        const s = snapshotSlot
        if (!s) return 'Start a new chain (no previous snapshot)'
        if (s.kind === 'after') return `After “${label(s.parent)}”`
        if (s.kind === 'edge') return `Between “${label(s.parent)}” and “${label(s.child)}”`
        return `New root before “${label(s.child)}”`
    }, [snapshotSlot, allSnapshots])

    const mySnapshots = useMemo(
        () => allSnapshots.filter((s) => {
            const m = s.metadata
            return !!detail && (m?.source_clone_name === detail.name || m?.source_clone_path === detail.path)
        }),
        [allSnapshots, detail],
    )
    const otherSnapshots = useMemo(
        () => allSnapshots.filter((s) => !mySnapshots.includes(s)),
        [allSnapshots, mySnapshots],
    )

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
                                <span className="text-xs font-medium text-muted-foreground">Name</span>
                                <button
                                    type="button"
                                    onClick={openEdit}
                                    aria-label="Edit name and description"
                                    className="rounded p-0.5 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                                >
                                    <Pencil className="size-3.5" />
                                </button>
                            </div>
                            <div className="text-[15px] font-medium text-zinc-100">
                                {detail.display_name?.trim() ? detail.display_name : <span className="text-muted-foreground">(unnamed clone)</span>}
                            </div>
                        </div>
                        {detail.description?.trim() && (
                            <div>
                                <div className="mb-1 text-xs font-medium text-muted-foreground">Description</div>
                                <div className="text-[13px] text-zinc-200">{detail.description}</div>
                            </div>
                        )}
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
                        {conn && (
                            <div>
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
                        )}
                    </div>

                    <div className="mt-4 flex flex-wrap gap-2">
                        <Button onClick={openRefresh} disabled={actionBusy || !detail.has_container}>Refresh</Button>
                        <Button onClick={openSnapshot} disabled={actionBusy || !detail.has_container}>Create Snapshot</Button>
                        <Button variant="destructive" onClick={() => setDeleteOpen(true)} disabled={actionBusy}>Delete</Button>
                    </div>
                </Card>
            )}

            <Card className="mt-4">
                <div className="flex items-start justify-between gap-3">
                    <div>
                        <CardTitle>Snapshots from this clone</CardTitle>
                        <p className="mb-2.5 mt-0.5 text-xs text-muted-foreground">
                            Lineage of snapshots taken from this clone. Select a snapshot to reset or move it.
                        </p>
                    </div>
                    {nodeActionFor && mySnapshots.some((s) => s.name === nodeActionFor) && (
                        <div className="flex flex-none items-center gap-2">
                            <Button variant="primary" onClick={() => { const n = nodeActionFor; setNodeActionFor(null); if (n) setResetFor(n) }} disabled={moveBusy}>
                                Reset to this snapshot
                            </Button>
                            <Button onClick={() => setNodeActionFor(null)} disabled={moveBusy}>Done</Button>
                        </div>
                    )}
                </div>
                <LineageGraph
                    items={mySnapshots}
                    mode="list"
                    draggable={false}
                    moveTarget={nodeActionFor}
                    onNodeClick={(s) => setNodeActionFor(s.name)}
                    onMove={onMoveSnapshot}
                    maxHeight={440}
                />
            </Card>

            <Card className="mt-4">
                <div className="flex items-start justify-between gap-3">
                    <div>
                        <CardTitle>Other snapshots</CardTitle>
                        <p className="mb-2.5 mt-0.5 text-xs text-muted-foreground">
                            Snapshots from other clones or main. Select a snapshot to reset or move it.
                        </p>
                    </div>
                    {nodeActionFor && otherSnapshots.some((s) => s.name === nodeActionFor) && (
                        <div className="flex flex-none items-center gap-2">
                            <Button variant="primary" onClick={() => { const n = nodeActionFor; setNodeActionFor(null); if (n) setResetFor(n) }} disabled={moveBusy}>
                                Reset to this snapshot
                            </Button>
                            <Button onClick={() => setNodeActionFor(null)} disabled={moveBusy}>Done</Button>
                        </div>
                    )}
                </div>
                <LineageGraph
                    items={otherSnapshots}
                    mode="list"
                    draggable={false}
                    moveTarget={nodeActionFor}
                    onNodeClick={(s) => setNodeActionFor(s.name)}
                    onMove={onMoveSnapshot}
                    maxHeight={440}
                />
            </Card>

            <Dialog open={refreshOpen} onOpenChange={(open) => { if (!actionBusy) setRefreshOpen(open) }}>
                <DialogContent>
                    <DialogTitle>Refresh clone</DialogTitle>
                    <DialogDescription>
                        This re-syncs the clone with the latest data from main and recreates its container.
                        The name and description are kept, and any changes made inside this clone are discarded.
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
                    <DialogTitle>Edit clone</DialogTitle>
                    <label className="grid gap-1.5">
                        <span className="text-[13px] text-muted-foreground">Name (required)</span>
                        <Input
                            autoFocus
                            value={editName}
                            onChange={(e) => setEditName(e.target.value)}
                            placeholder="e.g. feature-xyz"
                            className="w-full"
                        />
                    </label>
                    <label className="mt-3 grid gap-1.5">
                        <span className="text-[13px] text-muted-foreground">Description (optional)</span>
                        <Textarea
                            value={editDesc}
                            onChange={(e) => setEditDesc(e.target.value)}
                            placeholder="Notes about this clone"
                            className="min-h-[80px] font-sans text-[13px]"
                        />
                    </label>
                    <DialogFooter>
                        <Button onClick={() => setEditOpen(false)} disabled={actionBusy}>Cancel</Button>
                        <Button variant="primary" onClick={saveMeta} disabled={actionBusy || !editName.trim()}>
                            {actionBusy ? 'Saving...' : 'Save'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            <Dialog open={snapshotOpen} onOpenChange={(open) => { if (!actionBusy) setSnapshotOpen(open) }}>
                <DialogContent className="max-w-3xl max-h-[90vh] overflow-y-auto">
                    <DialogTitle>Create snapshot</DialogTitle>
                    <DialogDescription>
                        A read-only btrfs snapshot is captured from this clone's current state.
                    </DialogDescription>
                    <label className="mt-1 grid gap-1.5">
                        <span className="text-[13px] text-muted-foreground">Description (required)</span>
                        <Input
                            autoFocus
                            value={snapshotDesc}
                            onChange={(e) => setSnapshotDesc(e.target.value)}
                            placeholder="e.g. before-migration"
                            className="w-full"
                        />
                    </label>
                    <div className="mt-3">
                        <RetentionSelect value={snapshotRetention} onChange={setSnapshotRetention} />
                    </div>
                    <div className="mt-4 grid gap-1.5">
                        <span className="text-[13px] text-muted-foreground">Insertion point — click a <span className="text-primary">+</span> in the graph</span>
                        <LineageGraph
                            items={allSnapshots}
                            mode="insert"
                            selectedSlot={snapshotSlot}
                            onSelectSlot={setSnapshotSlot}
                            maxHeight={300}
                        />
                        <span className="text-xs text-muted-foreground">{slotSummary}</span>
                    </div>
                    <DialogFooter>
                        <Button onClick={() => setSnapshotOpen(false)} disabled={actionBusy}>Cancel</Button>
                        <Button variant="primary" onClick={confirmSnapshot} disabled={actionBusy || !snapshotDesc.trim()}>
                            {actionBusy ? 'Creating...' : 'Create Snapshot'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            <Dialog open={deleteOpen} onOpenChange={(open) => { if (!actionBusy) setDeleteOpen(open) }}>
                <DialogContent>
                    <DialogTitle>Delete clone</DialogTitle>
                    <DialogDescription>
                        The container and its btrfs subvolume will be deleted together. Snapshots taken from this clone are unaffected.
                    </DialogDescription>
                    <p className="mt-2 text-[13px]">
                        Target: <strong className="font-semibold">{detail?.display_name?.trim() || detail?.name}</strong>
                    </p>
                    <DialogFooter>
                        <Button onClick={() => setDeleteOpen(false)} disabled={actionBusy}>Cancel</Button>
                        <Button variant="destructive" onClick={confirmDeleteClone} disabled={actionBusy}>
                            {actionBusy ? 'Deleting...' : 'Delete'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>


            <Dialog open={!!resetFor} onOpenChange={(open) => { if (!open && !actionBusy) setResetFor(null) }}>
                <DialogContent>
                    <DialogTitle>Reset clone to snapshot</DialogTitle>
                    <DialogDescription>
                        The clone's container is recreated from this snapshot. Any changes in the clone since are discarded.
                    </DialogDescription>
                    <div className="mt-2 break-all rounded-md border border-border bg-secondary px-3 py-2 font-mono text-[12px] text-muted-foreground">
                        {resetFor}
                    </div>
                    <DialogFooter>
                        <Button onClick={() => setResetFor(null)} disabled={actionBusy}>Cancel</Button>
                        <Button variant="primary" onClick={confirmReset} disabled={actionBusy}>
                            {actionBusy ? 'Resetting...' : 'Reset to this snapshot'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </div>
    )
}
