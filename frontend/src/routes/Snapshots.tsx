import { useEffect, useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogTitle,
} from '@/components/ui/dialog'
import { useToast } from '@/components/ui/toast'
import { RetentionSelect } from '@/components/RetentionSelect'
import {
    LineageGraph,
    computeInsertParams,
    computeMoveUpdates,
    type Slot,
    type SnapshotItem,
} from '@/components/LineageGraph'

export function Snapshots() {
    const toast = useToast()
    const [items, setItems] = useState<SnapshotItem[]>([])
    const [loading, setLoading] = useState(false)
    const [error, setError] = useState<string | null>(null)

    const [createOpen, setCreateOpen] = useState(false)
    const [snapshotDesc, setSnapshotDesc] = useState('')
    const [snapshotSlot, setSnapshotSlot] = useState<Slot | null>(null)
    const [snapshotRetention, setSnapshotRetention] = useState(14)
    const [creating, setCreating] = useState(false)

    const [query, setQuery] = useState('')
    const [matchIdx, setMatchIdx] = useState(0)

    // the currently selected snapshot — drives the bottom action panel + move slots
    const [nodeFor, setNodeFor] = useState<SnapshotItem | null>(null)

    const [cloneFor, setCloneFor] = useState<SnapshotItem | null>(null)
    const [cloneName, setCloneName] = useState('')
    const [cloneDesc, setCloneDesc] = useState('')
    const [cloneBusy, setCloneBusy] = useState(false)
    const [cloneError, setCloneError] = useState<string | null>(null)

    const [deleteFor, setDeleteFor] = useState<SnapshotItem | null>(null)
    const [deleteBusy, setDeleteBusy] = useState(false)

    const api = import.meta.env.VITE_API_BASE_URL || ''
    const base = api ? api : '/api'

    const loadSnapshots = () => {
        setLoading(true)
        setError(null)
        fetch(`${base}/snapshots`)
            .then((r) => (r.ok ? r.json() : Promise.reject(r)))
            .then((data) => setItems(data))
            .catch(async (e) => {
                const text = e?.status ? `${e.status} ${await e.text()}` : String(e)
                setError(text)
            })
            .finally(() => setLoading(false))
    }

    useEffect(() => {
        loadSnapshots()
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])

    // ---- search (by description) ----
    const matches = useMemo(() => {
        const q = query.trim().toLowerCase()
        if (!q) return [] as string[]
        return items.filter((s) => (s.description || '').toLowerCase().includes(q)).map((s) => s.name)
    }, [items, query])
    useEffect(() => { setMatchIdx(0) }, [query])
    const highlight = matches.length ? matches[matchIdx % matches.length] : null

    // ---- create snapshot from main ----
    const openCreate = () => {
        setSnapshotDesc('')
        setSnapshotRetention(14)
        setSnapshotSlot(null)
        setError(null)
        setCreateOpen(true)
    }

    const slotSummary = () => {
        const label = (name: string) => items.find((s) => s.name === name)?.description?.trim() || name
        const s = snapshotSlot
        if (!s) return 'Start a new chain (no previous snapshot)'
        if (s.kind === 'after') return `After “${label(s.parent)}”`
        if (s.kind === 'edge') return `Between “${label(s.parent)}” and “${label(s.child)}”`
        return `New root before “${label(s.child)}”`
    }

    const onCreateSnapshot = async () => {
        const desc = snapshotDesc.trim()
        if (!desc) {
            setError('Snapshot description is required.')
            return
        }
        setCreating(true)
        setError(null)
        const tid = toast.loading('Creating snapshot from main…')
        const { previous_snapshot, insert_before } = snapshotSlot
            ? computeInsertParams(snapshotSlot)
            : { previous_snapshot: null, insert_before: null }
        try {
            const r = await fetch(`${base}/snapshots`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ description: desc, previous_snapshot, insert_before, retention_days: snapshotRetention }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const created: SnapshotItem = await r.json()
            toast.update(tid, 'success', `Snapshot created: ${created.name}`)
            setCreateOpen(false)
            loadSnapshots()
        } catch (e: any) {
            toast.update(tid, 'error', `Snapshot failed: ${String(e?.message || e)}`)
            setError(String(e?.message || e))
        } finally {
            setCreating(false)
        }
    }

    // ---- move a snapshot (drag, or select-then-place) ----
    const onMove = async (name: string, slot: Slot) => {
        const updates = computeMoveUpdates(items, name, slot)
        setNodeFor(null)
        if (!updates.length) return
        const tid = toast.loading('Moving snapshot…')
        try {
            const r = await fetch(`${base}/snapshots/lineage/batch`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ updates }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            toast.update(tid, 'success', `Lineage updated (${res.applied?.length ?? 0} change${(res.applied?.length ?? 0) === 1 ? '' : 's'})`)
            loadSnapshots()
        } catch (e: any) {
            toast.update(tid, 'error', `Re-order failed: ${String(e?.message || e)}`)
        }
    }

    // ---- node actions ----
    const openNode = (s: SnapshotItem) => setNodeFor(s)

    // ---- clone from snapshot ----
    const openClone = (s: SnapshotItem) => {
        setCloneFor(s)
        setCloneName('')
        setCloneDesc('')
        setCloneError(null)
    }

    const confirmClone = async () => {
        if (!cloneFor) return
        const name = cloneName.trim()
        if (!name) {
            setCloneError('Name is required.')
            return
        }
        setCloneBusy(true)
        setCloneError(null)
        const tid = toast.loading(`Cloning from snapshot…`)
        try {
            const r = await fetch(`${base}/snapshots/${encodeURIComponent(cloneFor.name)}/clone`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, description: cloneDesc.trim() }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            toast.update(tid, 'success', `Clone “${name}” created on port ${res.host_port}`)
            setCloneFor(null)
        } catch (e: any) {
            toast.update(tid, 'error', `Clone failed: ${String(e?.message || e)}`)
            setCloneError(String(e?.message || e))
        } finally {
            setCloneBusy(false)
        }
    }

    // ---- delete snapshot ----
    const confirmDelete = async () => {
        if (!deleteFor) return
        setDeleteBusy(true)
        const tid = toast.loading('Deleting snapshot…')
        try {
            const r = await fetch(`${base}/snapshots/${encodeURIComponent(deleteFor.name)}`, { method: 'DELETE' })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            toast.update(tid, 'success', 'Snapshot deleted')
            setDeleteFor(null)
            loadSnapshots()
        } catch (e: any) {
            toast.update(tid, 'error', `Delete failed: ${String(e?.message || e)}`)
        } finally {
            setDeleteBusy(false)
        }
    }

    return (
        <div className="mx-auto max-w-6xl animate-page-in px-6 pb-20 pt-6">
            <div className="mb-2 flex items-center justify-between gap-4 border-b border-border pb-4">
                <h1 className="text-base font-semibold tracking-tight">Snapshots</h1>
                <div className="flex items-center gap-2">
                    <Button variant="primary" onClick={openCreate}>Create snapshot</Button>
                    <Button onClick={loadSnapshots} disabled={loading}>
                        {loading ? 'Refreshing...' : 'Refresh'}
                    </Button>
                </div>
            </div>

            <Card className="mt-4">
                {error && <p className="mb-2 text-[13px] text-destructive">{error}</p>}

                <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                    {nodeFor ? (
                        <div className="flex min-w-0 items-center gap-2 text-[11px] text-muted-foreground">
                            <span className="size-1.5 flex-none rounded-full bg-warning" />
                            <span className="truncate text-[13px] font-medium text-zinc-100">{nodeFor.description?.trim() || nodeFor.name}</span>
                            <span className="flex-none">— click a <span className="text-primary">+</span> to move it</span>
                        </div>
                    ) : (
                        <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                            Lineage — select a snapshot to act on it or move it
                        </div>
                    )}
                    <div className="flex items-center gap-1.5">
                        {nodeFor ? (
                            <>
                                <Button onClick={() => { const s = nodeFor; setNodeFor(null); if (s) openClone(s) }}>Clone</Button>
                                <Button variant="destructive" onClick={() => { const s = nodeFor; setNodeFor(null); setDeleteFor(s) }}>Delete</Button>
                                <Button onClick={() => setNodeFor(null)}>Done</Button>
                            </>
                        ) : (
                            <>
                                <Input
                                    value={query}
                                    onChange={(e) => setQuery(e.target.value)}
                                    onKeyDown={(e) => { if (e.key === 'Enter' && matches.length) setMatchIdx((i) => i + 1) }}
                                    placeholder="Search description…"
                                    className="h-8 w-52 text-[13px]"
                                />
                                {query.trim() && (
                                    <span className="whitespace-nowrap text-[11px] text-muted-foreground">
                                        {matches.length ? `${(matchIdx % matches.length) + 1}/${matches.length}` : '0 results'}
                                    </span>
                                )}
                            </>
                        )}
                    </div>
                </div>

                <LineageGraph
                    items={items}
                    mode="list"
                    highlightName={highlight}
                    moveTarget={nodeFor?.name ?? null}
                    draggable={false}
                    onNodeClick={openNode}
                    onMove={onMove}
                    maxHeight={560}
                />
            </Card>

            {/* create snapshot from main modal */}
            <Dialog open={createOpen} onOpenChange={(open) => { if (!creating) setCreateOpen(open) }}>
                <DialogContent className="max-w-3xl max-h-[90vh] overflow-y-auto">
                    <DialogTitle>Create snapshot from main</DialogTitle>
                    <DialogDescription>
                        A read-only btrfs snapshot is captured from the current main data.
                    </DialogDescription>
                    <label className="mt-1 grid gap-1.5">
                        <span className="text-[13px] text-muted-foreground">Description (required)</span>
                        <Input
                            autoFocus
                            value={snapshotDesc}
                            onChange={(e) => setSnapshotDesc(e.target.value)}
                            placeholder="e.g. baseline before release"
                            className="w-full"
                        />
                    </label>
                    <div className="mt-3">
                        <RetentionSelect value={snapshotRetention} onChange={setSnapshotRetention} />
                    </div>
                    <div className="mt-4 grid gap-1.5">
                        <span className="text-[13px] text-muted-foreground">Insertion point — click a <span className="text-primary">+</span> in the graph</span>
                        <LineageGraph
                            items={items}
                            mode="insert"
                            selectedSlot={snapshotSlot}
                            onSelectSlot={setSnapshotSlot}
                            maxHeight={300}
                        />
                        <span className="text-xs text-muted-foreground">{slotSummary()}</span>
                    </div>
                    {error && <p className="mt-2 whitespace-pre-wrap text-[13px] text-destructive">{error}</p>}
                    <DialogFooter>
                        <Button onClick={() => setCreateOpen(false)} disabled={creating}>Cancel</Button>
                        <Button variant="primary" onClick={onCreateSnapshot} disabled={creating || !snapshotDesc.trim()}>
                            {creating ? 'Creating...' : 'Create Snapshot'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>


            {/* clone-from-snapshot modal */}
            <Dialog open={!!cloneFor} onOpenChange={(open) => { if (!open && !cloneBusy) setCloneFor(null) }}>
                <DialogContent>
                    <DialogTitle>Clone from snapshot</DialogTitle>
                    <DialogDescription>
                        A new writable clone with its own Postgres container is created from this snapshot.
                    </DialogDescription>
                    <div className="mt-2 break-all rounded-md border border-border bg-secondary px-3 py-2 font-mono text-[12px] text-muted-foreground">
                        {cloneFor?.name}
                    </div>
                    <label className="mt-3 grid gap-1.5">
                        <span className="text-[13px] text-muted-foreground">Name (required)</span>
                        <Input
                            autoFocus
                            value={cloneName}
                            onChange={(e) => setCloneName(e.target.value)}
                            placeholder="e.g. bugfix-1234"
                            className="w-full"
                        />
                    </label>
                    <label className="mt-3 grid gap-1.5">
                        <span className="text-[13px] text-muted-foreground">Description (optional)</span>
                        <Input
                            value={cloneDesc}
                            onChange={(e) => setCloneDesc(e.target.value)}
                            placeholder="e.g. reproducing the checkout bug"
                            className="w-full"
                        />
                    </label>
                    {cloneError && <p className="mt-2 whitespace-pre-wrap text-[13px] text-destructive">{cloneError}</p>}
                    <DialogFooter>
                        <Button onClick={() => setCloneFor(null)} disabled={cloneBusy}>Cancel</Button>
                        <Button variant="primary" onClick={confirmClone} disabled={cloneBusy || !cloneName.trim()}>
                            {cloneBusy ? 'Cloning...' : 'Create Clone'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            {/* delete modal */}
            <Dialog open={!!deleteFor} onOpenChange={(open) => { if (!open && !deleteBusy) setDeleteFor(null) }}>
                <DialogContent>
                    <DialogTitle>Delete snapshot</DialogTitle>
                    <DialogDescription>
                        This removes the snapshot subvolume. Clones already created from it are unaffected.
                    </DialogDescription>
                    <div className="mt-2 break-all rounded-md border border-border bg-secondary px-3 py-2 font-mono text-[12px] text-muted-foreground">
                        {deleteFor?.name}
                    </div>
                    <DialogFooter>
                        <Button onClick={() => setDeleteFor(null)} disabled={deleteBusy}>Cancel</Button>
                        <Button variant="destructive" onClick={confirmDelete} disabled={deleteBusy}>
                            {deleteBusy ? 'Deleting...' : 'Delete'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </div>
    )
}
