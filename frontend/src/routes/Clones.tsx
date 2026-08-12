import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { AlertTriangle, Check, Copy, Loader2, Star, Upload, X } from 'lucide-react'

import { cloneLabel } from '@/lib/cloneLabel'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { useToast } from '@/components/ui/toast'
import { cn, copyText } from '@/lib/utils'
import { RetentionSelect } from '@/components/RetentionSelect'
import { LineageGraph, computeInsertParams, type Slot, type SnapshotItem } from '@/components/LineageGraph'
import { BootstrapGate } from '@/components/BootstrapGate'

type CloneStageStatus = 'pending' | 'running' | 'done' | 'skipped' | 'failed'

interface CloneStage {
    key: string
    label: string
    status: CloneStageStatus
    ms: number | null
}

interface CloneProgress {
    active: boolean
    stage: string | null
    stage_started_at: number | null
    error: string | null
    stages: CloneStage[]
}

/** The itinerary of a clone build, while it is still being built.
 *
 * Every stage is listed from the start, including the ones not reached yet:
 * the wait is long enough that "what is left" is as much of the answer as
 * "where are we", and a list that grew a row at a time would keep the end out
 * of sight. A stage that turned out not to be needed is struck through rather
 * than removed, so the rows do not move under the reader. */
function CloneStages({ progress }: { progress: CloneProgress }) {
    const secs = (ms: number) => (ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`)
    return (
        <div className="grid gap-1 rounded-md border border-border bg-secondary/40 px-3 py-2.5">
            {progress.stages.map((s) => (
                <div key={s.key} className="flex items-center gap-2 text-[12px]">
                    <span className="flex size-3.5 flex-none items-center justify-center">
                        {s.status === 'done' && <Check className="size-3 text-success" />}
                        {s.status === 'running' && <Loader2 className="size-3 animate-spin text-info" />}
                        {s.status === 'failed' && <X className="size-3 text-destructive" />}
                    </span>
                    <span
                        className={cn(
                            s.status === 'running' && 'font-medium text-foreground',
                            s.status === 'done' && 'text-muted-foreground',
                            s.status === 'pending' && 'text-muted-foreground/50',
                            s.status === 'skipped' && 'text-muted-foreground/40 line-through',
                            s.status === 'failed' && 'text-destructive',
                        )}
                    >
                        {s.label}
                    </span>
                    {s.ms != null && (
                        <span className="ml-auto tabular-nums text-[11px] text-muted-foreground/60">{secs(s.ms)}</span>
                    )}
                </div>
            ))}
        </div>
    )
}

interface CloneItem {
    name: string
    path: string
    is_btrfs: boolean
    has_container: boolean
    container_name: string | null
    container_status: string | null
    container_ports: string | null
    host_port?: number | null
    is_running: boolean
    container_started_at: string | null
    display_name?: string | null
    description?: string | null
    db_user?: string | null
    db_password?: string | null
    db_name?: string | null
}

interface CloneSnapshotOption {
    name: string
    description?: string | null
}

export function Clones() {
    const navigate = useNavigate()
    const toast = useToast()
    const [clones, setClones] = useState<CloneItem[]>([])
    const [clonesLoading, setClonesLoading] = useState(false)
    const [clonesError, setClonesError] = useState<string | null>(null)
    const [message, setMessage] = useState<string | null>(null)
    const [error, setError] = useState<string | null>(null)

    // The clone, not its container name: the dialog has to say which clone
    // this is, and the container name is not what the reader called it.
    const [deleting, setDeleting] = useState<CloneItem | null>(null)
    const [deletingBusy, setDeletingBusy] = useState(false)

    const [createOpen, setCreateOpen] = useState(false)
    const [anonOpen, setAnonOpen] = useState(false)
    const [anonPath, setAnonPath] = useState('')
    const [anonSql, setAnonSql] = useState('')
    const [anonFile, setAnonFile] = useState('')
    const [anonLines, setAnonLines] = useState(0)
    const [anonDragging, setAnonDragging] = useState(false)
    const [anonSaving, setAnonSaving] = useState(false)
    const [anonError, setAnonError] = useState<string | null>(null)

    // Read here rather than posting the file: the script is stored as text and
    // the endpoint already takes text, so a multipart upload would only add a
    // second way in. The checks are the ones a wrong file actually fails —
    // empty, or so large it is not a script.
    const readAnonFile = (f: File) => {
        setAnonError(null)
        if (f.size > 1_000_000) {
            setAnonError(`${f.name} is ${(f.size / 1_000_000).toFixed(1)} MB — that is not an anonymization script.`)
            return
        }
        const r = new FileReader()
        r.onerror = () => setAnonError(`Could not read ${f.name}`)
        r.onload = () => {
            const text = String(r.result || '')
            if (!text.trim()) {
                setAnonError(`${f.name} is empty`)
                return
            }
            setAnonSql(text)
            setAnonFile(f.name)
            setAnonLines(text.trimEnd().split('\n').length)
        }
        r.readAsText(f)
    }
    const [createName, setCreateName] = useState('')
    const [createDesc, setCreateDesc] = useState('')
    const [createPort, setCreatePort] = useState('')
    const [createUser, setCreateUser] = useState('')
    const [createPw, setCreatePw] = useState('')
    const [createError, setCreateError] = useState<string | null>(null)
    const [mainCloning, setMainCloning] = useState(false)
    const [cloneProgress, setCloneProgress] = useState<CloneProgress | null>(null)
    const defaultUser = 'snaplicator'
    const [refreshingClone, setRefreshingClone] = useState<string | null>(null)
    const [refreshFor, setRefreshFor] = useState<CloneItem | null>(null)
    const [copiedClone, setCopiedClone] = useState<string | null>(null)

    const [snapshotFor, setSnapshotFor] = useState<CloneItem | null>(null)
    const [snapshotDesc, setSnapshotDesc] = useState('')
    const [snapshotSlot, setSnapshotSlot] = useState<Slot | null>(null)
    const [snapshotRetention, setSnapshotRetention] = useState(14)
    const [allSnapshots, setAllSnapshots] = useState<SnapshotItem[]>([])
    const [snapshotBusy, setSnapshotBusy] = useState(false)
    const [snapshotError, setSnapshotError] = useState<string | null>(null)

    const connHost = (typeof window !== 'undefined' && window.location.hostname) || 'localhost'
    const buildConnUrl = (c: CloneItem, masked: boolean) =>
        `postgresql://${c.db_user ?? ''}:${masked ? '••••••••' : (c.db_password ?? '')}@${connHost}:${c.host_port ?? ''}/${c.db_name ?? ''}`

    const onCopyUrl = async (c: CloneItem) => {
        const ok = await copyText(buildConnUrl(c, false))
        if (!ok) {
            setError('Copy failed. Select the connection string and copy manually.')
            return
        }
        setCopiedClone(c.name)
        setTimeout(() => setCopiedClone((v) => (v === c.name ? null : v)), 1500)
    }

    const openSnapshot = async (c: CloneItem) => {
        setSnapshotFor(c)
        setSnapshotDesc('')
        setSnapshotError(null)
        setSnapshotRetention(14)
        setSnapshotSlot(null)
        setAllSnapshots([])
        try {
            const [allR, cloneR] = await Promise.all([
                fetch(`${base}/snapshots`),
                fetch(`${base}/clones/${encodeURIComponent(c.name)}/snapshots`),
            ])
            if (allR.ok) setAllSnapshots(await allR.json())
            if (cloneR.ok) {
                const snaps: CloneSnapshotOption[] = await cloneR.json()
                // default insertion = right after this clone's most recent snapshot
                if (snaps.length) setSnapshotSlot({ kind: 'after', parent: snaps[snaps.length - 1].name })
            }
        } catch {
            /* graph just stays empty */
        }
    }

    const confirmSnapshot = async () => {
        if (!snapshotFor) return
        const desc = snapshotDesc.trim()
        if (!desc) {
            setSnapshotError('Description is required.')
            return
        }
        setSnapshotBusy(true)
        setSnapshotError(null)
        setMessage(null)
        setError(null)
        const tid = toast.loading('Creating snapshot…')
        const { previous_snapshot, insert_before } = snapshotSlot
            ? computeInsertParams(snapshotSlot)
            : { previous_snapshot: null, insert_before: null }
        try {
            const r = await fetch(`${base}/clones/${encodeURIComponent(snapshotFor.name)}/snapshots`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ description: desc, previous_snapshot, insert_before, retention_days: snapshotRetention }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            toast.update(tid, 'success', `Snapshot created: ${res.name}`)
            setSnapshotFor(null)
        } catch (e: any) {
            toast.update(tid, 'error', `Snapshot failed: ${String(e?.message || e)}`)
            setSnapshotError(String(e?.message || e))
        } finally {
            setSnapshotBusy(false)
        }
    }

    const FAV_KEY = 'snaplicator.favoriteClones'
    const [favorites, setFavorites] = useState<Set<string>>(() => {
        try {
            return new Set<string>(JSON.parse(localStorage.getItem(FAV_KEY) || '[]'))
        } catch {
            return new Set<string>()
        }
    })
    const toggleFavorite = (name: string) => {
        setFavorites((prev) => {
            const next = new Set(prev)
            if (next.has(name)) next.delete(name)
            else next.add(name)
            localStorage.setItem(FAV_KEY, JSON.stringify(Array.from(next)))
            return next
        })
    }

    const api = import.meta.env.VITE_API_BASE_URL || ''
    const base = api ? api : '/api'

    const loadClones = () => {
        setClonesLoading(true)
        setClonesError(null)
        fetch(`${base}/clones`)
            .then((r) => (r.ok ? r.json() : Promise.reject(r)))
            .then((data) => setClones(data))
            .catch(async (e) => {
                const text = e?.status ? `${e.status} ${await e.text()}` : String(e)
                setClonesError(text)
            })
            .finally(() => setClonesLoading(false))
    }

    useEffect(() => {
        loadClones()
        // eslint-disable-next-line react-hooks-exhaustive-deps
    }, [])

    // Asked before the clone exists, not after. A clone made without an
    // anonymization script is a copy of production with a port on it, and the
    // moment to say so is while it is still a decision.
    const onCreateClone = async () => {
        const trimmedName = createName.trim()
        const user = createUser.trim()
        const pw = createPw
        if (!trimmedName) {
            setCreateError('Name is required.')
            return
        }
        if (!!user !== !!pw) {
            setCreateError('Username and password must be provided together.')
            return
        }
        try {
            const r = await fetch(`${base}/clones/anonymize-sql`)
            if (r.ok) {
                const d = await r.json()
                setAnonPath(d.path || '')
                if (!d.configured) {
                    setAnonOpen(true)
                    return
                }
            }
        } catch {
            /* unreachable check is not a reason to block a clone */
        }
        await doCreateClone()
    }

    const doCreateClone = async () => {
        const trimmedName = createName.trim()
        const trimmedDesc = createDesc.trim()
        const user = createUser.trim()
        const pw = createPw
        setAnonOpen(false)
        setMainCloning(true)
        setCreateError(null)
        setMessage(null)
        setError(null)
        const tid = toast.loading(`Creating clone “${trimmedName}”…`)
        // The POST below does not return until the clone is built, so where it
        // has got to has to be asked for separately. Only a record still
        // marked active is read: the previous build's leftovers describe a
        // clone that already exists, and this runs for the length of one POST
        // that nothing else overlaps.
        const stopProgress = followProgress(tid, trimmedName)
        try {
            const portNum = createPort.trim() ? parseInt(createPort.trim(), 10) : undefined
            const bodyData: Record<string, unknown> = { name: trimmedName, description: trimmedDesc, port: portNum }
            if (user) {
                bodyData.username = user
                bodyData.password = pw
            }
            const r = await fetch(`${base}/clones`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(bodyData),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            toast.update(tid, 'success', `Clone “${trimmedName}” created on port ${res.host_port}`)
            setCreateOpen(false)
            setCreateName('')
            setCreateDesc('')
            setCreatePort('')
            setCreateUser('')
            setCreatePw('')
            loadClones()
        } catch (e: any) {
            toast.update(tid, 'error', `Clone failed: ${String(e?.message || e)}`)
            setCreateError(String(e?.message || e))
        } finally {
            stopProgress()
            setMainCloning(false)
        }
    }

    // Where a long clone operation has got to, pushed into the toast that is
    // already sitting in the corner. The POST does not return until the work
    // is done, so the only way to say anything meanwhile is to ask a second
    // endpoint — and the toast is where it belongs, because closing the
    // dialog should not close the answer.
    const followProgress = (tid: number, label: string) => {
        setCloneProgress(null)
        const poll = window.setInterval(async () => {
            try {
                const pr = await fetch(`${base}/clones/create-progress`)
                if (!pr.ok) return
                const p: CloneProgress = await pr.json()
                // Only a record still marked active: a finished one describes
                // the previous operation, not this one.
                if (!p?.active) return
                setCloneProgress(p)
                const at = p.stages.findIndex((s) => s.status === 'running')
                if (at >= 0) {
                    toast.update(tid, 'loading', `${label}: ${p.stages[at].label} (${at + 1}/${p.stages.length})`)
                }
            } catch {
                /* the progress read is decoration; its failure is not the operation's */
            }
        }, 1000)
        return () => { window.clearInterval(poll); setCloneProgress(null) }
    }

    const onDelete = (clone: CloneItem) => {
        setDeleting(clone)
        setMessage(null)
        setError(null)
    }

    const confirmDelete = async () => {
        if (!deleting) return
        setDeletingBusy(true)
        const tid = toast.loading('Deleting clone…')
        try {
            // The API is addressed by container name; the reader is told the
            // clone's name. The two are not the same string and only one of
            // them belongs on screen.
            const target = deleting.container_name || deleting.name
            const r = await fetch(`${base}/clones/${encodeURIComponent(target)}`, { method: 'DELETE' })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            await r.json()
            toast.update(tid, 'success', `Deleted ${cloneLabel(deleting)}`)
            loadClones()
            setDeleting(null)
        } catch (e: any) {
            toast.update(tid, 'error', `Delete failed: ${String(e?.message || e)}`)
            setError(String(e?.message || e))
        } finally {
            setDeletingBusy(false)
        }
    }

    const onRefreshClone = (clone: CloneItem) => {
        const targetName = clone.container_name || clone.name
        if (!targetName || !clone.has_container) {
            setClonesError('Cannot refresh a clone without a running container.')
            return
        }
        setRefreshFor(clone)
    }

    const confirmRefreshClone = async () => {
        if (!refreshFor) return
        const targetName = refreshFor.container_name || refreshFor.name
        setRefreshingClone(targetName)
        setMessage(null)
        setError(null)
        setClonesError(null)
        const tid = toast.loading('Refreshing clone from main…')
        const stopProgress = followProgress(tid, cloneLabel(refreshFor))
        try {
            const r = await fetch(`${base}/clones/${encodeURIComponent(targetName)}/refresh`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            toast.update(tid, 'success', `Refreshed ${cloneLabel(refreshFor)}`)
            setRefreshFor(null)
            loadClones()
        } catch (e: any) {
            toast.update(tid, 'error', `Refresh failed: ${String(e?.message || e)}`)
            setError(String(e?.message || e))
        } finally {
            stopProgress()
            setRefreshingClone(null)
        }
    }

    const snapshotSlotSummary = () => {
        const label = (name: string) => allSnapshots.find((s) => s.name === name)?.description?.trim() || name
        const s = snapshotSlot
        if (!s) return 'Start a new chain (no previous snapshot)'
        if (s.kind === 'after') return `After “${label(s.parent)}”`
        if (s.kind === 'edge') return `Between “${label(s.parent)}” and “${label(s.child)}”`
        return `New root before “${label(s.child)}”`
    }

    const renderClone = (c: CloneItem) => {
        const targetName = c.container_name || c.name
        const isFav = favorites.has(c.name)
        const running = c.has_container && c.is_running
        const goDetail = () => navigate(`/clones/${encodeURIComponent(targetName)}`)
        return (
            <li
                key={c.path}
                role="button"
                tabIndex={0}
                onClick={goDetail}
                onKeyDown={(e) => {
                    if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) {
                        e.preventDefault()
                        goDetail()
                    }
                }}
                className={cn(
                    'flex cursor-pointer items-center gap-3 rounded-md border bg-secondary px-3.5 py-2.5 transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                    isFav ? 'border-warning/35' : 'border-border hover:border-border-strong',
                )}
            >
                <button
                    className={cn(
                        'flex-none rounded p-1 transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                        isFav ? 'text-warning' : 'text-muted-foreground hover:text-warning',
                    )}
                    onClick={(e) => { e.stopPropagation(); toggleFavorite(c.name) }}
                    title={isFav ? 'Remove from favorites' : 'Add to favorites'}
                    aria-label="toggle favorite"
                >
                    <Star className="size-3.5" fill={isFav ? 'currentColor' : 'none'} />
                </button>
                <div className="grid min-w-0 flex-1 gap-1">
                    <div className="flex min-w-0 items-center gap-2">
                        <span
                            className={cn('size-1.5 flex-none rounded-full', running ? 'bg-success' : 'bg-zinc-600')}
                            title={running ? 'running' : 'stopped'}
                        />
                        <span className="min-w-0 truncate text-[13px] font-medium text-zinc-100">
                            {c.display_name?.trim() || c.description?.trim() ? cloneLabel(c) : <span className="text-muted-foreground">{cloneLabel(c)}</span>}
                        </span>
                    </div>
                    <div className="flex min-w-0 items-center gap-1.5">
                        <code className="min-w-0 truncate font-mono text-[12px] text-muted-foreground">{buildConnUrl(c, true)}</code>
                        <button
                            onClick={(e) => { e.stopPropagation(); onCopyUrl(c) }}
                            title="Copy connection string"
                            aria-label="Copy connection string"
                            className="flex-none rounded p-0.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        >
                            {copiedClone === c.name ? <Check className="size-3.5 text-success" /> : <Copy className="size-3.5" />}
                        </button>
                    </div>
                </div>
                <div className="ml-auto flex flex-none gap-2">
                    <Button
                        onClick={(e) => { e.stopPropagation(); openSnapshot(c) }}
                        disabled={!c.has_container}
                        title={c.has_container ? 'Create a snapshot from this clone' : 'No container to snapshot.'}
                    >
                        Snapshot
                    </Button>
                    <Button
                        onClick={(e) => { e.stopPropagation(); onRefreshClone(c) }}
                        disabled={refreshingClone === targetName || !c.has_container}
                        title={c.has_container ? 'Replace the container data with the latest from main' : 'No container to refresh.'}
                    >
                        {refreshingClone === targetName ? 'Refreshing...' : 'Refresh'}
                    </Button>
                    <Button variant="destructive" onClick={(e) => { e.stopPropagation(); onDelete(c) }} disabled={deletingBusy}>
                        Delete
                    </Button>
                </div>
            </li>
        )
    }

    return (
        <div className="mx-auto max-w-5xl animate-page-in px-6 pb-20 pt-6">
            <div className="mb-2 flex items-center justify-between gap-4 border-b border-border pb-4">
                <h1 className="text-base font-semibold tracking-tight">Clones</h1>
                <div className="flex items-center gap-2">
                    <Button onClick={() => { setCreateError(null); setCreateOpen(true) }}>
                        New Clone
                    </Button>
                    <Button onClick={loadClones} disabled={clonesLoading}>
                        {clonesLoading ? 'Refreshing...' : 'Refresh'}
                    </Button>
                </div>
            </div>

            <BootstrapGate onDone={loadClones} />

            <Dialog open={anonOpen} onOpenChange={(open) => { if (!mainCloning && !anonSaving) setAnonOpen(open) }}>
                <DialogContent className="max-w-lg">
                    <DialogTitle className="flex items-center gap-2">
                        <AlertTriangle className="h-4 w-4 text-warning" />
                        No anonymization script
                    </DialogTitle>
                    <DialogDescription className="leading-relaxed">
                        Every clone runs this script before anyone can connect to it. There is none, so
                        this clone would be your production data with a port on it — real names, real
                        emails, real everything, in whatever it gets used for.
                    </DialogDescription>

                    <div className="mt-4">
                        <div className="mb-1.5 flex items-baseline justify-between gap-3">
                            <span className="text-[13px] font-medium">Upload one</span>
                            {anonPath && (
                                <span className="truncate font-mono text-[11px] text-muted-foreground" title={anonPath}>
                                    {anonPath}
                                </span>
                            )}
                        </div>

                        {/* A file, because that is where this script already
                            lives: it is written against a schema, reviewed, and
                            kept in a repository. Retyping it into a box is not
                            a step anybody takes. */}
                        <label
                            onDragOver={(e) => { e.preventDefault(); setAnonDragging(true) }}
                            onDragLeave={() => setAnonDragging(false)}
                            onDrop={(e) => {
                                e.preventDefault()
                                setAnonDragging(false)
                                const f = e.dataTransfer.files?.[0]
                                if (f) readAnonFile(f)
                            }}
                            className={cn(
                                'flex cursor-pointer flex-col items-center justify-center gap-1 rounded-md border border-dashed px-4 py-6 text-center transition-colors',
                                anonDragging ? 'border-primary bg-primary/[0.06]' : 'border-border-strong hover:bg-white/[0.03]',
                            )}
                        >
                            <input
                                type="file"
                                accept=".sql,text/plain"
                                className="hidden"
                                onChange={(e) => {
                                    const f = e.target.files?.[0]
                                    if (f) readAnonFile(f)
                                    e.target.value = ''
                                }}
                            />
                            <Upload className="h-4 w-4 text-muted-foreground" />
                            {anonFile ? (
                                <>
                                    <span className="font-mono text-[13px]">{anonFile}</span>
                                    <span className="text-xs text-muted-foreground">
                                        {anonLines} line{anonLines === 1 ? '' : 's'} · click or drop to replace
                                    </span>
                                </>
                            ) : (
                                <>
                                    <span className="text-[13px]">Choose a .sql file, or drop one here</span>
                                    <span className="text-xs text-muted-foreground">
                                        Runs inside every clone made after this, not just this one
                                    </span>
                                </>
                            )}
                        </label>

                        {anonError && <p className="mt-1.5 text-xs text-destructive">{anonError}</p>}
                    </div>

                    <DialogFooter className="mt-4 flex-col items-stretch gap-2 sm:flex-col">
                        <Button
                            variant="primary"
                            disabled={!anonSql.trim() || anonSaving || mainCloning}
                            onClick={async () => {
                                setAnonSaving(true)
                                setAnonError(null)
                                try {
                                    const r = await fetch(`${base}/clones/anonymize-sql`, {
                                        method: 'PUT',
                                        headers: { 'Content-Type': 'application/json' },
                                        body: JSON.stringify({ sql: anonSql }),
                                    })
                                    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
                                    await doCreateClone()
                                } catch (e: any) {
                                    setAnonError(String(e?.message || e))
                                } finally {
                                    setAnonSaving(false)
                                }
                            }}
                        >
                            {anonSaving ? 'Saving…' : anonFile ? `Save ${anonFile} and create clone` : 'Save script and create clone'}
                        </Button>

                        {/* Second, and worded as what it does rather than what
                            it skips: the risk is the point of the dialog. */}
                        <div className="rounded-md border border-warning/30 bg-warning/[0.07] p-2.5">
                            <p className="mb-2 text-xs leading-relaxed">
                                Creating without one means <span className="font-medium text-warning">production data
                                will be what people test against</span> — anyone with the clone's connection
                                string can read it, and copies of it spread with every snapshot taken from it.
                            </p>
                            <Button
                                size="sm"
                                disabled={mainCloning || anonSaving}
                                onClick={() => doCreateClone()}
                            >
                                {mainCloning ? 'Cloning…' : 'Create anyway, without anonymization'}
                            </Button>
                        </div>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            <Card className="mt-4">
                {clonesError && <p className="mb-2 text-[13px] text-destructive">{clonesError}</p>}
                {message && <p className="mb-2 text-[13px] text-success">{message}</p>}
                {error && <p className="mb-2 text-[13px] text-destructive">{error}</p>}

                {clones.some((c) => favorites.has(c.name)) && (
                    <>
                        <div className="mb-1.5 mt-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                            Favorites
                        </div>
                        <ul className="grid gap-2">
                            {clones.filter((c) => favorites.has(c.name)).map(renderClone)}
                        </ul>
                        <div className="mb-1.5 mt-4 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                            All clones
                        </div>
                    </>
                )}
                <ul className="mt-2 grid gap-2">
                    {clones.length === 0 && (
                        <li className="rounded-md border border-border bg-secondary px-3.5 py-2.5 text-muted-foreground">
                            No clones
                        </li>
                    )}
                    {clones.filter((c) => !favorites.has(c.name)).map(renderClone)}
                </ul>
            </Card>

            <Dialog open={createOpen} onOpenChange={(open) => { if (!mainCloning) setCreateOpen(open) }}>
                <DialogContent>
                    <DialogTitle>New clone from main</DialogTitle>
                    <div className="grid gap-3">
                        <label className="grid gap-1.5">
                            <span className="text-[13px] text-muted-foreground">Name (required)</span>
                            <Input
                                autoFocus
                                value={createName}
                                onChange={(e) => setCreateName(e.target.value)}
                                placeholder="e.g. feature-xyz"
                                className="w-full"
                            />
                        </label>
                        <label className="grid gap-1.5">
                            <span className="text-[13px] text-muted-foreground">Description (optional)</span>
                            <Input
                                value={createDesc}
                                onChange={(e) => setCreateDesc(e.target.value)}
                                placeholder="e.g. testing the new checkout flow"
                                className="w-full"
                            />
                        </label>
                        <label className="grid gap-1.5">
                            <span className="text-[13px] text-muted-foreground">Port (auto-assigned if empty)</span>
                            <Input
                                value={createPort}
                                onChange={(e) => setCreatePort(e.target.value)}
                                placeholder="e.g. 5440"
                                className="w-full"
                            />
                        </label>
                        <div className="grid grid-cols-2 gap-3">
                            <label className="grid gap-1.5">
                                <span className="text-[13px] text-muted-foreground">Username (default: {defaultUser})</span>
                                <Input
                                    value={createUser}
                                    onChange={(e) => setCreateUser(e.target.value)}
                                    placeholder={defaultUser}
                                    className="w-full"
                                />
                            </label>
                            <label className="grid gap-1.5">
                                <span className="text-[13px] text-muted-foreground">Password (default: {defaultUser})</span>
                                <Input
                                    type="password"
                                    value={createPw}
                                    onChange={(e) => setCreatePw(e.target.value)}
                                    placeholder="••••••••"
                                    className="w-full"
                                />
                            </label>
                        </div>
                        <DialogDescription className="text-xs leading-relaxed">
                            Leave Username/Password empty to connect with the default account{' '}
                            <code className="rounded bg-secondary px-1 py-0.5 font-mono text-[11px]">{defaultUser}</code>{' '}
                            and its default password. If provided, the account is created in this clone.
                        </DialogDescription>
                        {mainCloning && cloneProgress && <CloneStages progress={cloneProgress} />}
                        {createError && <p className="whitespace-pre-wrap text-[13px] text-destructive">{createError}</p>}
                    </div>
                    <DialogFooter>
                        <Button onClick={() => setCreateOpen(false)} disabled={mainCloning}>Cancel</Button>
                        <Button variant="primary" onClick={onCreateClone} disabled={mainCloning || !createName.trim()}>
                            {mainCloning ? 'Cloning...' : 'Create Clone'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            <Dialog open={!!snapshotFor} onOpenChange={(open) => { if (!open && !snapshotBusy) setSnapshotFor(null) }}>
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
                        <span className="text-xs text-muted-foreground">{snapshotSlotSummary()}</span>
                    </div>
                    {snapshotError && <p className="whitespace-pre-wrap text-[13px] text-destructive">{snapshotError}</p>}
                    <DialogFooter>
                        <Button onClick={() => setSnapshotFor(null)} disabled={snapshotBusy}>Cancel</Button>
                        <Button variant="primary" onClick={confirmSnapshot} disabled={snapshotBusy || !snapshotDesc.trim()}>
                            {snapshotBusy ? 'Creating...' : 'Create Snapshot'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            <Dialog open={!!deleting} onOpenChange={(open) => { if (!open && !deletingBusy) setDeleting(null) }}>
                <DialogContent>
                    <DialogTitle>Delete clone</DialogTitle>
                    <DialogDescription>
                        The container and its btrfs subvolume will be deleted together.
                    </DialogDescription>
                    <p className="mt-2 text-[13px]">
                        Target: <strong className="font-semibold">{cloneLabel(deleting)}</strong>
                    </p>
                    <DialogFooter>
                        <Button onClick={() => setDeleting(null)} disabled={deletingBusy}>Cancel</Button>
                        <Button variant="destructive" onClick={confirmDelete} disabled={deletingBusy}>
                            {deletingBusy ? 'Deleting...' : 'Delete'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            <Dialog open={!!refreshFor} onOpenChange={(open) => { if (!open && refreshingClone === null) setRefreshFor(null) }}>
                <DialogContent>
                    <DialogTitle>Refresh clone</DialogTitle>
                    <DialogDescription>
                        This re-syncs the clone with the latest data from main and recreates its container.
                        The name and description are kept, and any changes made inside this clone are discarded.
                    </DialogDescription>
                    <p className="mt-2 text-[13px]">
                        Target: <strong className="font-semibold">{cloneLabel(refreshFor)}</strong>
                    </p>
                    <DialogFooter>
                        <Button onClick={() => setRefreshFor(null)} disabled={refreshingClone !== null}>Cancel</Button>
                        <Button variant="primary" onClick={confirmRefreshClone} disabled={refreshingClone !== null}>
                            {refreshingClone !== null ? 'Refreshing...' : 'Refresh'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </div>
    )
}
