import { useEffect, useMemo, useRef, useState } from 'react'

import { cn } from '@/lib/utils'

export interface SnapMeta {
    source_clone_display_name?: string | null
    source_clone_name?: string | null
    source_clone_path?: string | null
    previous_snapshot?: string | null
    next_snapshot?: string | null
    type?: string | null
    created_at?: string | null
    retention_days?: number | null
    expires_at?: string | null
}

export interface SnapshotItem {
    name: string
    path: string
    readonly: boolean
    description?: string | null
    metadata?: SnapMeta | null
}

// A drop/insert target in the lineage graph. Everything reduces to re-pointing
// `previous_snapshot`, so these three kinds describe every position.
export type Slot =
    | { kind: 'edge'; parent: string; child: string } // insert between parent -> child
    | { kind: 'after'; parent: string } // append / new branch after parent
    | { kind: 'before-root'; child: string } // new root in front of child

export interface LineageUpdate {
    snapshot: string
    previous_snapshot: string | null
}

// ---- geometry ----
const NODE_W = 200
const NODE_H = 64
const COL_W = 276
const ROW_H = 98
const PAD = 24
const SLOT_OFF = 22 // how far a slot sits from the node edge

export function sourceLabel(s: SnapshotItem): string | null {
    const m = s.metadata
    if (m?.source_clone_display_name?.trim()) return m.source_clone_display_name.trim()
    if (m?.type === 'main_snapshot') return 'main'
    return null
}

export function formatTs(iso?: string | null): string {
    if (!iso) return ''
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return ''
    const p = (n: number) => String(n).padStart(2, '0')
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export function retentionLabel(m?: SnapMeta | null): string {
    if (!m) return 'kept ∞'
    if (m.retention_days === 0) return 'kept ∞'
    if (!m.expires_at) return 'kept ∞'
    const d = new Date(m.expires_at)
    if (Number.isNaN(d.getTime())) return 'kept ∞'
    const days = Math.ceil((d.getTime() - Date.now()) / 86_400_000)
    if (days < 0) return 'expired'
    if (days === 0) return 'expires today'
    return `expires ${days}d`
}

export function slotsEqual(a?: Slot | null, b?: Slot | null): boolean {
    if (!a || !b || a.kind !== b.kind) return false
    if (a.kind === 'after' && b.kind === 'after') return a.parent === b.parent
    if (a.kind === 'edge' && b.kind === 'edge') return a.parent === b.parent && a.child === b.child
    if (a.kind === 'before-root' && b.kind === 'before-root') return a.child === b.child
    return false
}

function slotInvolves(slot: Slot, name: string): boolean {
    if (slot.kind === 'after') return slot.parent === name
    if (slot.kind === 'edge') return slot.parent === name || slot.child === name
    return slot.child === name
}

function prevMap(items: SnapshotItem[]): Map<string, string | null> {
    const has = new Set(items.map((s) => s.name))
    const m = new Map<string, string | null>()
    items.forEach((s) => {
        const p = s.metadata?.previous_snapshot
        m.set(s.name, p && has.has(p) ? p : null)
    })
    return m
}

// Drag-to-move: drop dragName onto slot. Heal the gap dragName leaves (its
// children reconnect to its old parent), then splice dragName at the slot.
export function computeMoveUpdates(items: SnapshotItem[], dragName: string, slot: Slot): LineageUpdate[] {
    const prev = prevMap(items)
    const oldPrev = prev.get(dragName) ?? null
    const out: LineageUpdate[] = []
    items.forEach((s) => {
        if ((prev.get(s.name) ?? null) === dragName) out.push({ snapshot: s.name, previous_snapshot: oldPrev })
    })
    if (slot.kind === 'after') {
        out.push({ snapshot: dragName, previous_snapshot: slot.parent })
    } else if (slot.kind === 'edge') {
        out.push({ snapshot: dragName, previous_snapshot: slot.parent })
        out.push({ snapshot: slot.child, previous_snapshot: dragName })
    } else {
        out.push({ snapshot: dragName, previous_snapshot: null })
        out.push({ snapshot: slot.child, previous_snapshot: dragName })
    }
    // dedupe by snapshot, last assignment wins
    const map = new Map<string, string | null>()
    out.forEach((u) => map.set(u.snapshot, u.previous_snapshot))
    return [...map.entries()].map(([snapshot, previous_snapshot]) => ({ snapshot, previous_snapshot }))
}

// Insert-on-create: translate a slot into the create payload.
export function computeInsertParams(slot: Slot): { previous_snapshot: string | null; insert_before: string | null } {
    if (slot.kind === 'after') return { previous_snapshot: slot.parent, insert_before: null }
    if (slot.kind === 'edge') return { previous_snapshot: slot.parent, insert_before: slot.child }
    return { previous_snapshot: null, insert_before: slot.child }
}

interface Positioned {
    snap: SnapshotItem
    col: number
    row: number
    prev: string | null
}

function useLayout(items: SnapshotItem[]) {
    return useMemo(() => {
        const byName = new Map(items.map((s) => [s.name, s]))
        const prevOf = (s: SnapshotItem) => {
            const p = s.metadata?.previous_snapshot
            return p && byName.has(p) ? p : null
        }
        const children = new Map<string, string[]>()
        items.forEach((s) => {
            const p = prevOf(s)
            if (p) {
                const arr = children.get(p) ?? []
                arr.push(s.name)
                children.set(p, arr)
            }
        })
        children.forEach((arr) => arr.sort())

        const pos = new Map<string, { col: number; row: number }>()
        const placing = new Set<string>()
        let nextRow = 0
        const place = (name: string, col: number): number => {
            const ex = pos.get(name)
            if (ex) return ex.row
            if (placing.has(name)) {
                const r = nextRow++
                pos.set(name, { col, row: r })
                return r
            }
            placing.add(name)
            const kids = children.get(name) ?? []
            const row = kids.length === 0 ? nextRow++ : kids.map((k) => place(k, col + 1))[0]
            pos.set(name, { col, row })
            placing.delete(name)
            return row
        }
        items.filter((s) => !prevOf(s)).map((s) => s.name).sort().forEach((r) => place(r, 0))
        items.forEach((s) => { if (!pos.has(s.name)) place(s.name, 0) })

        let maxCol = 0
        let maxRow = 0
        pos.forEach((p) => { maxCol = Math.max(maxCol, p.col); maxRow = Math.max(maxRow, p.row) })

        const positioned: Positioned[] = items.map((s) => {
            const p = pos.get(s.name) ?? { col: 0, row: 0 }
            return { snap: s, col: p.col, row: p.row, prev: prevOf(s) }
        })
        const left = (col: number) => PAD + col * COL_W
        const top = (row: number) => PAD + row * ROW_H

        const edges = positioned
            .filter((n) => n.prev)
            .map((n) => {
                const p = pos.get(n.prev!)!
                const x1 = left(p.col) + NODE_W
                const y1 = top(p.row) + NODE_H / 2
                const x2 = left(n.col)
                const y2 = top(n.row) + NODE_H / 2
                return { id: n.snap.name, d: `M ${x1} ${y1} C ${x1 + 32} ${y1}, ${x2 - 32} ${y2}, ${x2} ${y2}` }
            })

        return {
            positioned,
            edges,
            posMap: pos,
            left,
            top,
            width: PAD * 2 + maxCol * COL_W + NODE_W + COL_W, // extra room for right-side slots
            height: PAD * 2 + maxRow * ROW_H + NODE_H,
        }
    }, [items])
}

interface LineageGraphProps {
    items: SnapshotItem[]
    mode: 'list' | 'insert'
    highlightName?: string | null
    // list mode
    onNodeClick?: (s: SnapshotItem) => void
    onMove?: (name: string, slot: Slot) => void
    // list mode: when set, the graph is in "placement" mode for this node — slots
    // are shown and clicking one calls onMove(moveTarget, slot). Drag also works.
    moveTarget?: string | null
    // insert mode
    selectedSlot?: Slot | null
    onSelectSlot?: (slot: Slot) => void
    className?: string
    maxHeight?: number
    // list mode: set false for a read-only display (nodes still clickable, no drag/slots)
    draggable?: boolean
}

export function LineageGraph({
    items,
    mode,
    highlightName,
    onNodeClick,
    onMove,
    moveTarget,
    selectedSlot,
    onSelectSlot,
    className,
    maxHeight,
    draggable = true,
}: LineageGraphProps) {
    const { positioned, edges, left, top, width, height } = useLayout(items)
    const scrollRef = useRef<HTMLDivElement>(null)
    const [dragName, setDragName] = useState<string | null>(null)
    const [hoverSlot, setHoverSlot] = useState<Slot | null>(null)

    const canDrag = mode === 'list' && draggable
    // the node currently being placed: a live drag, or a parent-driven selection
    const activeName = dragName ?? moveTarget ?? null
    const placing = mode === 'list' && moveTarget != null
    const showSlots = mode === 'insert' || activeName != null

    // nodes that are someone's parent (have at least one child) — used to dedupe slots
    const parentNames = useMemo(() => {
        const names = new Set(items.map((s) => s.name))
        const set = new Set<string>()
        items.forEach((s) => {
            const p = s.metadata?.previous_snapshot
            if (p && names.has(p)) set.add(p)
        })
        return set
    }, [items])

    // auto-scroll to highlighted node + briefly ring it
    useEffect(() => {
        if (!highlightName) return
        const n = positioned.find((p) => p.snap.name === highlightName)
        const el = scrollRef.current
        if (!n || !el) return
        const nodeLeft = left(n.col)
        const nodeTop = top(n.row)
        el.scrollTo({
            left: Math.max(0, nodeLeft - el.clientWidth / 2 + NODE_W / 2),
            top: Math.max(0, nodeTop - el.clientHeight / 2 + NODE_H / 2),
            behavior: 'smooth',
        })
    }, [highlightName, positioned, left, top])

    const applySlot = (slot: Slot) => {
        if (mode === 'insert') {
            onSelectSlot?.(slot)
            return
        }
        // list mode: a drop / placement click completes a move
        if (activeName && onMove && !slotInvolves(slot, activeName)) {
            onMove(activeName, slot)
        }
        setDragName(null)
        setHoverSlot(null)
    }

    // left slot of a node = insert before it (edge if it has a parent, else new root)
    const leftSlotOf = (n: Positioned): Slot =>
        n.prev ? { kind: 'edge', parent: n.prev, child: n.snap.name } : { kind: 'before-root', child: n.snap.name }
    const rightSlotOf = (n: Positioned): Slot => ({ kind: 'after', parent: n.snap.name })

    const slotDisabled = (slot: Slot) => mode === 'list' && activeName != null && slotInvolves(slot, activeName)

    const renderSlot = (slot: Slot, x: number, y: number, key: string) => {
        if (slotDisabled(slot)) return null
        const selected = mode === 'insert' && slotsEqual(slot, selectedSlot)
        const hovered = slotsEqual(slot, hoverSlot)
        return (
            <button
                key={key}
                type="button"
                onClick={() => applySlot(slot)}
                onDragOver={(e) => { if (dragName) { e.preventDefault(); setHoverSlot(slot) } }}
                onDragLeave={() => setHoverSlot((s) => (slotsEqual(s, slot) ? null : s))}
                onDrop={(e) => { e.preventDefault(); applySlot(slot) }}
                title={slot.kind === 'edge' ? 'Insert between' : slot.kind === 'after' ? 'Add after' : 'New root before'}
                style={{ left: x - 11, top: y - 11 }}
                className={cn(
                    'absolute z-20 flex size-[22px] items-center justify-center rounded-full border text-[13px] leading-none transition-all',
                    selected
                        ? 'border-primary bg-primary text-primary-foreground shadow-md shadow-primary/30'
                        : hovered
                            ? 'border-primary bg-primary/80 text-primary-foreground scale-110'
                            : 'border-dashed border-primary/60 bg-card/90 text-primary hover:border-primary hover:bg-primary/20',
                )}
            >
                +
            </button>
        )
    }

    if (items.length === 0) {
        return (
            <div className={cn('rounded-md border border-border bg-secondary px-3.5 py-6 text-center text-[13px] text-muted-foreground', className)}>
                {mode === 'insert' ? 'No snapshots yet — this will be the first one.' : 'No snapshots'}
            </div>
        )
    }

    return (
        <div
            ref={scrollRef}
            className={cn('overflow-auto rounded-md border border-border bg-[#0b0c0e]', className)}
            style={{ maxHeight }}
        >
            <div className="relative" style={{ width, height }}>
                <svg className="absolute inset-0 z-0" width={width} height={height}>
                    {edges.map((e) => (
                        <path
                            key={e.id}
                            d={e.d}
                            fill="none"
                            stroke={hoverSlot && hoverSlot.kind === 'edge' && hoverSlot.child === e.id ? '#5e6ad2' : '#5e6ad2'}
                            strokeOpacity={hoverSlot && hoverSlot.kind === 'edge' && hoverSlot.child === e.id ? 0.95 : 0.5}
                            strokeWidth={hoverSlot && hoverSlot.kind === 'edge' && hoverSlot.child === e.id ? 2.5 : 1.5}
                        />
                    ))}
                </svg>

                {/* slots */}
                {showSlots && positioned.map((n) => {
                    const lx = left(n.col)
                    const ty = top(n.row)
                    const cy = ty + NODE_H / 2
                    return (
                        <span key={`slots-${n.snap.name}`}>
                            {renderSlot(leftSlotOf(n), lx - SLOT_OFF, cy, `L-${n.snap.name}`)}
                            {/* one move point per edge: a node with children already exposes the
                                in-between slots as its children's left slots, so only leaves get an "after" slot */}
                            {!parentNames.has(n.snap.name) && renderSlot(rightSlotOf(n), lx + NODE_W + SLOT_OFF, cy, `R-${n.snap.name}`)}
                        </span>
                    )
                })}

                {/* nodes */}
                {positioned.map((n) => {
                    const lx = left(n.col)
                    const ty = top(n.row)
                    const src = sourceLabel(n.snap)
                    const isRoot = !n.prev
                    const isHighlight = highlightName === n.snap.name
                    const isDragging = dragName === n.snap.name
                    const isMoving = moveTarget === n.snap.name
                    const ret = retentionLabel(n.snap.metadata)
                    return (
                        <div
                            key={n.snap.name}
                            role="button"
                            tabIndex={0}
                            draggable={canDrag}
                            onDragStart={(e) => { if (canDrag) { setDragName(n.snap.name); e.dataTransfer.effectAllowed = 'move' } }}
                            onDragEnd={() => { setDragName(null); setHoverSlot(null) }}
                            onClick={() => { if (isDragging) return; onNodeClick?.(n.snap) }}
                            onKeyDown={(e) => { if (e.key === 'Enter') onNodeClick?.(n.snap) }}
                            style={{ left: lx, top: ty, width: NODE_W, height: NODE_H }}
                            className={cn(
                                'absolute z-10 flex flex-col justify-center gap-0.5 rounded-md border bg-secondary px-3 text-left transition-colors',
                                canDrag && 'cursor-grab active:cursor-grabbing',
                                onNodeClick && 'cursor-pointer',
                                onNodeClick && 'hover:border-primary/60 hover:bg-accent',
                                isMoving ? 'border-warning ring-2 ring-warning/70' : isHighlight ? 'border-purple ring-2 ring-purple/70' : 'border-border',
                                isDragging && 'opacity-40',
                                placing && !isMoving && 'opacity-70',
                            )}
                        >
                            <div className="flex items-center gap-1.5 pr-16">
                                <span className={cn('size-1.5 flex-none rounded-full', isRoot ? 'bg-purple' : 'bg-primary')} />
                                <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-zinc-100">
                                    {n.snap.description?.trim() || '(no description)'}
                                </span>
                            </div>
                            <div className="flex items-center gap-1.5 pl-3 text-[11px] text-muted-foreground">
                                <span className="truncate">{src ? `from ${src}` : n.snap.name}</span>
                                {formatTs(n.snap.metadata?.created_at) && (
                                    <span className="flex-none tabular-nums text-zinc-500">· {formatTs(n.snap.metadata?.created_at)}</span>
                                )}
                            </div>
                            <span
                                className={cn(
                                    'absolute right-1.5 top-1.5 rounded border px-1 py-px text-[10px] font-medium leading-none',
                                    ret === 'expired'
                                        ? 'border-destructive/40 bg-destructive/10 text-destructive'
                                        : ret.startsWith('expires')
                                            ? 'border-warning/40 bg-warning/10 text-warning'
                                            : 'border-border bg-card text-zinc-400',
                                )}
                                title="Retention"
                            >
                                {ret}
                            </span>
                        </div>
                    )
                })}
            </div>
        </div>
    )
}
