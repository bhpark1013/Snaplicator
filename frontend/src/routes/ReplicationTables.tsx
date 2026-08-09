import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronDown, ChevronRight, Eye, EyeOff, Radio, Search } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'
import { BootstrapGate } from '@/components/BootstrapGate'

interface TableInfo {
    schema: string
    table: string
    in_publication: boolean
    pub_via: 'table' | 'schema' | null
    in_subscriber: boolean
    estimated_rows: number
}

interface ConnInfo {
    publisher: { host: string; port: number; db: string; user: string; password: string }
    subscriber: { container: string; host: string; port: number; db: string; user: string; password: string }
    publication_name: string
    subscription_name: string
}

interface FdwState {
    server: { name: string; options: Record<string, string> }
    schemas: { name: string }[]
    tables: { schema: string; name: string }[]
    live_foreign_tables: { schema: string; table: string }[]
    yaml_path: string
    sql_path: string
    credentials?: {
        configured: boolean
        source: 'env' | 'ui' | 'none'
        user: string | null
        host: string | null
        port: number | null
        dbname: string | null
    }
}

type FilterTab = 'all' | 'replicated' | 'fdw' | 'none'

type TableMode = 'replicated' | 'fdw' | 'none'

// Resolve the table's effective sync mode. Publication + FDW are mutually
// exclusive (enforced server-side), so a single label captures the state.
//
// `noPublication` is the case where the primary has none at all: then not
// being published is not a decision anyone made, and showing every table as
// Excluded would contradict what this page promises — that everything starts
// in and you take things out. The publication is written when the copy
// starts, so until then the default is what is on screen.
function tableMode(t: TableInfo, fdwSet: Set<string>, noPublication = false): TableMode {
    if (fdwSet.has(`${t.schema}.${t.table}`)) return 'fdw'
    if (t.in_publication) return 'replicated'
    return noPublication ? 'replicated' : 'none'
}

const SYNC_KIND_LABEL: Record<string, string> = {
    table_added: 'Table added',
    column_added: 'Column added',
    check_constraint: 'CHECK constraint synced',
    schema_move: 'Schema move',
    fdw_drift: 'FDW re-import',
    trigger_reinstalled: 'Trigger reinstalled',
    loop_error: 'Sync error',
}

function fmtSync(e: any): { label: string; tone: 'ok' | 'warn' | 'err'; lines: string[] } {
    const d = (e && e.detail) || {}
    const label = SYNC_KIND_LABEL[e.kind] || e.kind
    const lines: string[] = []
    let tone: 'ok' | 'warn' | 'err' = 'ok'
    const errCount = Array.isArray(d.errors) ? d.errors.length : 0
    switch (e.kind) {
        case 'table_added': {
            const ss = d.synced || []
            lines.push(`${ss.length} table(s) reflected: ${ss.join(', ')}`)
            if (d.refreshed) lines.push('Subscription refreshed')
            break
        }
        case 'column_added': {
            const cc = d.columns_added || []
            lines.push(`${cc.length} column(s) added`)
            for (const x of cc) lines.push(`· ${x.table}.${x.column} (${x.type})`)
            break
        }
        case 'check_constraint': {
            const cs = d.constraints_synced || []
            lines.push(`${cs.length} constraint(s) synced`)
            for (const x of cs) lines.push(`· ${x.table}.${x.constraint} — ${x.action}`)
            break
        }
        case 'schema_move': {
            const moved = d.moved || []
            const orph = d.orphans || []
            const skip = d.skipped || []
            for (const m of moved) lines.push(`Moved: ${m.table} (${m.from} → ${m.to})`)
            if (orph.length) {
                tone = 'warn'
                for (const o of orph) lines.push(`Orphan (manual cleanup): ${o.table} — ${(o.subscriber_orphan_schemas || []).join(', ')}`)
            }
            if (skip.length) {
                tone = 'warn'
                for (const sk of skip) lines.push(`Skipped: ${sk.table} — ${sk.reason}`)
            }
            if (!lines.length) lines.push('No change')
            break
        }
        case 'fdw_drift': {
            const dr = d.drifted || []
            lines.push(`FDW re-IMPORT: ${dr.join(', ')}`)
            if (d.reapplied) lines.push('Re-applied')
            break
        }
        case 'trigger_reinstalled': {
            lines.push(`Auto-add trigger reinstalled (publication: ${d.publication || '-'})`)
            break
        }
        case 'loop_error': {
            tone = 'err'
            lines.push(String(d.error || 'Unknown error'))
            break
        }
        default:
            lines.push(JSON.stringify(d))
    }
    if (errCount) {
        tone = 'err'
        lines.push(`${errCount} error(s)`)
    }
    return { label, tone, lines }
}

const TONE_VARIANT = { ok: 'success', warn: 'warning', err: 'destructive' } as const

function formatRows(n: number) {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
    return String(n)
}

const MODE_LABEL: Record<TableMode, string> = {
    replicated: 'Replicate',
    fdw: 'Live',
    none: 'Exclude',
}

function splitFqn(fqn: string) {
    const [schema, ...rest] = fqn.split('.')
    return { schema, name: rest.join('.') }
}

// Name, state, size — shared by the schema row and its tables, so the three
// columns are the same three columns all the way down and the state of a
// schema sits directly above the state of the tables it contains.
const ROW_GRID = 'grid-cols-[minmax(0,1fr)_168px_52px]'

const MODE_CHIP: Record<TableMode, string> = {
    replicated: 'bg-success/12 text-success',
    fdw: 'bg-purple/15 text-purple',
    none: 'bg-white/[0.05] text-muted-foreground',
}

const MODE_STATE: Record<TableMode, string> = {
    replicated: 'Replicated',
    fdw: 'Live',
    none: 'Excluded',
}

/**
 * The three things a table can be, as one control.
 *
 * Not a checkbox: "replicate or not" was never the question — a table can
 * also be read live, and those two are alternatives rather than a flag with
 * an extra.
 *
 * `value` is null on a schema whose tables disagree; it reads as Mixed, and
 * pressing one option settles the whole schema.
 */
function ModeSwitch({
    value,
    fdwReady,
    locked,
    onChange,
    onNeedsFdw,
}: {
    value: TableMode | null
    fdwReady: boolean
    /** Why this cannot be changed, when it cannot. */
    locked?: string
    onChange: (mode: TableMode) => void
    onNeedsFdw: () => void
}) {
    const opts: { mode: TableMode; label: string }[] = [
        { mode: 'replicated', label: 'Replicate' },
        { mode: 'fdw', label: 'Live' },
        { mode: 'none', label: 'Exclude' },
    ]
    // At rest a row states what it is; under the cursor it offers what it
    // could be. Showing all three on every row means a list of eight tables
    // renders twenty-four words of controls against eight of content, and the
    // controls win — which is backwards, since only one of the three is ever
    // true and the other two are an offer nobody is currently taking up.
    //
    // Both layers occupy the same box, so nothing moves when they swap.
    // A publication this install promised not to rewrite has no draft to
    // make: the offer would be taken up and then refused by the server, which
    // is how this page came to show three buttons that all returned 409.
    if (locked) {
        return (
            <div className="relative flex h-5 items-center" title={locked} onClick={(e) => e.stopPropagation()}>
                {value ? (
                    <span className={cn('rounded px-1.5 py-0.5 text-[11.5px] leading-5 opacity-60', MODE_CHIP[value])}>
                        {MODE_STATE[value]}
                    </span>
                ) : (
                    <span className="px-1.5 text-[11.5px] leading-5 text-muted-foreground/70">Mixed</span>
                )}
            </div>
        )
    }

    return (
        <div className="relative h-5" onClick={(e) => e.stopPropagation()}>
            <div className="absolute inset-0 flex items-center transition-opacity group-hover:opacity-0 group-focus-within:opacity-0">
                {value ? (
                    <span className={cn('rounded px-1.5 py-0.5 text-[11.5px] leading-5', MODE_CHIP[value])}>
                        {MODE_STATE[value]}
                    </span>
                ) : (
                    <span className="px-1.5 text-[11.5px] leading-5 text-muted-foreground/70">Mixed</span>
                )}
            </div>

            <div className="pointer-events-none absolute inset-0 flex items-center gap-0.5 opacity-0 transition-opacity group-hover:pointer-events-auto group-hover:opacity-100 group-focus-within:pointer-events-auto group-focus-within:opacity-100">
                {opts.map((o) => {
                    // Live without a reader role is not refused, it is
                    // answered: pressing it asks for the one missing thing. A
                    // disabled control makes the user go and find out why
                    // elsewhere — the same work with an extra step.
                    const needsSetup = o.mode === 'fdw' && !fdwReady
                    const active = value === o.mode
                    return (
                        <button
                            key={o.mode}
                            title={needsSetup ? 'Live reads need a read-only account on the primary — click to set one up' : undefined}
                            onClick={() => (needsSetup ? onNeedsFdw() : onChange(o.mode))}
                            className={cn(
                                'rounded px-1.5 py-0.5 text-[11.5px] leading-5 transition-colors',
                                active ? MODE_CHIP[o.mode] : 'text-muted-foreground hover:bg-white/[0.06] hover:text-foreground',
                            )}
                        >
                            {o.label}
                        </button>
                    )
                })}
            </div>
        </div>
    )
}

/**
 * What happens to tables that do not exist yet, per schema.
 *
 * Its own control because it is its own question, and the two answers are
 * independent: a schema with tables left out can still want the next one, and
 * a schema with nothing left out can still want to stay exactly as it is.
 *
 * Written as a sentence at the foot of the schema's tables rather than as a
 * chip in its header. It is a rule about things that are not on screen, so it
 * has no row of its own to sit against and nothing to be read in relation to
 * — a two-word toggle up in the header is a label without its subject.
 */
function FollowRow({ on, locked, onChange }: { on: boolean; locked?: string; onChange: (v: boolean) => void }) {
    // Once per schema, so it has to be quiet: at full sentence weight, four
    // schemas put four copies of the same sentence down the page and the rule
    // shouts louder than the tables it applies to. Short at rest, explained
    // under the cursor.
    return (
        <button
            disabled={!!locked}
            onClick={(e) => { e.stopPropagation(); if (!locked) onChange(!on) }}
            title={locked
                ? locked
                : on
                    ? 'Tables created in this schema later join on their own — click to stop'
                    : 'Tables created in this schema later are left out — click to include them'}
            className={cn(
                'group/f flex w-full items-center gap-1 rounded-md py-0.5 pl-[26px] pr-2 text-left text-[11px] text-muted-foreground/45 transition-colors',
                !locked && 'hover:bg-white/[0.03] hover:text-muted-foreground',
            )}
        >
            <span>new tables →</span>
            <span className={cn(on ? 'text-success/70' : 'text-muted-foreground/60')}>
                {on ? 'replicated' : 'left out'}
            </span>
            {!locked && (
                <span className="opacity-0 transition-opacity group-hover/f:opacity-100">
                    · click to {on ? 'leave them out' : 'replicate them'}
                </span>
            )}
        </button>
    )
}

/** A titled section that stays out of the way until asked for. */
function Disclosure({
    title,
    summary,
    defaultOpen = false,
    children,
}: {
    title: string
    summary?: React.ReactNode
    defaultOpen?: boolean
    children: React.ReactNode
}) {
    const [open, setOpen] = useState(defaultOpen)
    // Same outline as the schemas below it: a line of text that opens, not a
    // box. Four boxes stacked down a page read as four unrelated things.
    return (
        <div className="mt-1">
            <button
                onClick={() => setOpen(!open)}
                className="group flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-white/[0.035]"
            >
                {open
                    ? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground/70" />
                    : <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground/70" />}
                <span className="text-[13px] font-semibold">{title}</span>
                <span className="ml-auto truncate pl-3 text-xs text-muted-foreground">{summary}</span>
            </button>
            {open && <div className="ml-[15px] border-l border-white/[0.11] py-1 pl-4">{children}</div>}
        </div>
    )
}

/**
 * The FDW login, entered here instead of in a file on the host.
 *
 * The password is checked against the primary before it is kept: the other
 * place to discover a wrong one is inside the replica, as a foreign table
 * that errors on every query, and a login that was never going to work should
 * not become part of the replica's catalog. It goes in and does not come back
 * out — the API answers whether one is set, never what it is.
 */
function FdwCredentialsForm({ base, onSaved }: { base: string; onSaved: () => void }) {
    const [user, setUser] = useState('')
    const [password, setPassword] = useState('')
    const [advanced, setAdvanced] = useState(false)
    const [host, setHost] = useState('')
    const [port, setPort] = useState('')
    const [dbname, setDbname] = useState('')
    const [busy, setBusy] = useState(false)
    const [err, setErr] = useState<string | null>(null)

    const save = async () => {
        setBusy(true)
        setErr(null)
        try {
            const r = await fetch(`${base}/replication/fdw/credentials`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    user,
                    password,
                    host: host || null,
                    port: port ? Number(port) : null,
                    dbname: dbname || null,
                }),
            })
            const body = await r.json().catch(() => ({}))
            if (!r.ok) throw new Error(body?.detail || `${r.status}`)
            setPassword('')
            onSaved()
        } catch (e: any) {
            setErr(String(e?.message || e))
        } finally {
            setBusy(false)
        }
    }

    return (
        <div>
            <div className="grid grid-cols-2 gap-2">
                <label className="flex min-w-0 flex-col gap-1.5">
                    <span className="text-xs text-muted-foreground">Role</span>
                    <Input value={user} onChange={(e) => setUser(e.target.value)} placeholder="snap_fdw" autoFocus />
                </label>
                <label className="flex min-w-0 flex-col gap-1.5">
                    <span className="text-xs text-muted-foreground">Password</span>
                    <Input
                        type="password"
                        value={password}
                        onChange={(e) => setPassword(e.target.value)}
                        placeholder="••••••••"
                        onKeyDown={(e) => { if (e.key === 'Enter' && user && password && !busy) save() }}
                    />
                </label>
            </div>

            {/* min-w-0 on every cell: a grid item's minimum is its content's,
                and an <input> carries an intrinsic width from its size
                attribute — so without this a 1fr column refuses to shrink
                below it and the last field hangs off the edge of the dialog. */}
            {advanced && (
                <div className="mt-2 grid grid-cols-[1fr_72px_1fr] gap-2">
                    <label className="flex min-w-0 flex-col gap-1.5">
                        <span className="text-xs text-muted-foreground">Host</span>
                        <Input value={host} onChange={(e) => setHost(e.target.value)} placeholder="as replication" />
                    </label>
                    <label className="flex min-w-0 flex-col gap-1.5">
                        <span className="text-xs text-muted-foreground">Port</span>
                        <Input value={port} onChange={(e) => setPort(e.target.value)} placeholder="5432" />
                    </label>
                    <label className="flex min-w-0 flex-col gap-1.5">
                        <span className="text-xs text-muted-foreground">Database</span>
                        <Input value={dbname} onChange={(e) => setDbname(e.target.value)} placeholder="as replication" />
                    </label>
                </div>
            )}

            <div className="mt-2.5 flex items-center gap-2">
                <Button variant="ghost" size="sm" onClick={() => setAdvanced((a) => !a)}>
                    {advanced ? 'Same host as replication' : 'Reads go somewhere else?'}
                </Button>
                <Button variant="primary" size="sm" className="ml-auto" disabled={!user || !password || busy} onClick={save}>
                    {busy ? 'Checking…' : 'Save and connect'}
                </Button>
            </div>

            {err && <p className="mt-2.5 text-xs text-destructive">{err}</p>}
        </div>
    )
}

/**
 * Everything about live reads, in the place the question is asked from.
 *
 * The panel this replaces sat above the list, which meant the answer to
 * "why can't I press Live" lived two sections away from Live. Here the
 * control opens the explanation, and what it explains is the only thing
 * standing between the press and the result.
 */
function LiveDialog({
    open,
    onOpenChange,
    base,
    fdw,
    fdwReady,
    target,
    onConfigured,
}: {
    open: boolean
    onOpenChange: (open: boolean) => void
    base: string
    fdw: FdwState | null
    fdwReady: boolean
    target: string | null
    onConfigured: () => void
}) {
    const opts = fdw?.server?.options || {}
    const reads = opts.host ? `${opts.host}:${opts.port || 5432}/${opts.dbname || ''}` : '—'
    const creds = fdw?.credentials

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-w-lg">
                <DialogTitle className="mb-1 flex items-center gap-2">
                    <Radio className="h-3.5 w-3.5 text-purple" />
                    Live reads
                </DialogTitle>
                <DialogDescription className="leading-relaxed">
                    Read from the primary on every query — always current, never copied, as fast as the
                    link. A table is live or replicated, never both.
                </DialogDescription>

                {fdwReady ? (
                    <>
                        <dl className="mt-4 grid grid-cols-[88px_1fr] gap-y-1.5 text-[13px]">
                            <dt className="text-muted-foreground">Reads from</dt>
                            <dd className="truncate font-mono">{reads}</dd>
                            <dt className="text-muted-foreground">As</dt>
                            <dd className="font-mono">{creds?.user || opts.user || '—'}</dd>
                            <dt className="text-muted-foreground">Server</dt>
                            <dd className="font-mono">{fdw?.server?.name || '—'}</dd>
                            <dt className="text-muted-foreground">Live tables</dt>
                            <dd className="font-mono">{fdw?.live_foreign_tables?.length || 0}</dd>
                        </dl>

                        {fdw?.schemas?.length ? (
                            <div className="mt-3">
                                <div className="mb-1.5 text-xs text-muted-foreground">Whole schemas imported</div>
                                <div className="flex flex-wrap gap-1">
                                    {fdw.schemas.map((s) => <Badge key={s.name} variant="purple">{s.name}</Badge>)}
                                </div>
                            </div>
                        ) : null}

                        <DialogFooter>
                            <Button onClick={() => onOpenChange(false)}>Close</Button>
                        </DialogFooter>
                    </>
                ) : (
                    <>
                        {target && (
                            <div className="mt-3 rounded-md border border-purple/25 bg-purple/[0.07] px-3 py-2 text-[13px]">
                                <span className="font-mono">{target}</span>
                                <span className="text-muted-foreground"> goes live as soon as this is set.</span>
                            </div>
                        )}

                        <p className="mt-4 text-[13px] leading-relaxed text-muted-foreground">
                            <span className="font-medium text-foreground">Needs its own read-only account.</span>{' '}
                            The login lives in the replica's catalog, and every clone copies it — so anyone
                            you hand a clone to can query as that account.
                        </p>

                        <div className="mt-4">
                            <div className="mb-1.5 flex items-center gap-2 text-xs text-muted-foreground">
                                <span className="flex h-4 w-4 items-center justify-center rounded-full bg-secondary font-mono text-[10px] text-foreground">1</span>
                                On the primary, a role that can only read
                            </div>
                            <pre className="overflow-x-auto rounded-md border border-border bg-secondary/60 p-2.5 font-mono text-[11.5px] leading-relaxed">{`CREATE ROLE snap_fdw LOGIN PASSWORD '…';
GRANT USAGE ON SCHEMA public TO snap_fdw;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO snap_fdw;`}</pre>
                        </div>

                        <div className="mt-4">
                            <div className="mb-2 flex items-center gap-2 text-xs text-muted-foreground">
                                <span className="flex h-4 w-4 items-center justify-center rounded-full bg-secondary font-mono text-[10px] text-foreground">2</span>
                                Tell Snaplicator about it
                            </div>
                            <FdwCredentialsForm base={base} onSaved={onConfigured} />
                        </div>

                        <p className="mt-3 border-t border-border pt-3 text-xs leading-relaxed text-muted-foreground">
                            Verified against the primary before it is saved.
                        </p>
                    </>
                )}
            </DialogContent>
        </Dialog>
    )
}

/**
 * What the publication on the primary already covers, per schema.
 *
 * The install used to settle this before anyone had seen a table name, and a
 * publication inherited from somewhere else is exactly the case where "start
 * replicating" should not be pressed on trust. The list further down says the
 * same thing table by table; this says it in one screenful, which is what
 * confirming needs.
 */
function PublicationCoverage({ tables }: { tables: TableInfo[] }) {
    const [open, setOpen] = useState(false)
    const rows = useMemo(() => {
        const by = new Map<string, { total: number; inPub: number }>()
        for (const t of tables) {
            const e = by.get(t.schema) || { total: 0, inPub: 0 }
            e.total++
            if (t.in_publication) e.inPub++
            by.set(t.schema, e)
        }
        return Array.from(by.entries())
            .map(([schema, v]) => ({ schema, ...v }))
            .sort((a, b) => a.schema.localeCompare(b.schema))
    }, [tables])

    if (!rows.length) return null
    const partial = rows.filter((r) => r.inPub > 0 && r.inPub < r.total).length
    const none = rows.filter((r) => r.inPub === 0).length

    return (
        <div className="mt-2">
            <button
                onClick={() => setOpen((v) => !v)}
                className="flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
            >
                {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                {open ? 'Hide' : 'Show'} what it covers
                {!open && (partial > 0 || none > 0) && (
                    <span className="text-warning/80">
                        · {[partial && `${partial} partial`, none && `${none} not covered`].filter(Boolean).join(', ')}
                    </span>
                )}
            </button>
            {open && (
                <div className="ml-4 mt-1.5 grid gap-x-6 gap-y-0.5 text-xs sm:grid-cols-2">
                    {rows.map((r) => (
                        <div key={r.schema} className="flex items-baseline gap-2">
                            <span className="truncate font-mono">{r.schema}</span>
                            <span className="ml-auto shrink-0 font-mono text-muted-foreground">
                                <span className={r.inPub === 0 ? 'text-muted-foreground' : r.inPub < r.total ? 'text-warning' : 'text-success'}>
                                    {r.inPub}
                                </span>
                                /{r.total}
                            </span>
                        </div>
                    ))}
                </div>
            )}
        </div>
    )
}

type PubRow = { name: string; all_tables: boolean; table_count: number; ours: boolean; active: boolean }

const gib = (n: number | null) =>
    n == null ? '—' : `${(n / 1024 ** 3).toFixed(1)} GiB`

type PubMode = 'reuse' | 'adopt' | 'create'

/** The question the reader actually arrives with: this primary's, or mine?
 *
 * Whether an existing publication is read as it stands or taken over matters
 * enormously — one is harmless, the other can cut off another replica — but it
 * is not the first thing anyone wants to decide, and putting all three answers
 * on one level made the page ask about consequences before it had established
 * the subject. Same split the installer offers: open what is here, or add
 * something of your own. */
type PubBranch = 'existing' | 'new'

/** One of the answers, with whatever it needs asked underneath it.
 *
 * Declared here rather than inside the chooser because a component defined in
 * another component's body is a new type on every render, and React replaces
 * a subtree whose type changed rather than updating it. The text field below
 * would be torn down and rebuilt on each keystroke, losing focus after one
 * character — a name field that cannot be typed into.
 *
 * The label covers the radio and its text and stops there. A click anywhere
 * in a label is forwarded to its control, so a field inside one would hand
 * its clicks to the radio instead of taking the caret. */
function ChoiceCard<T extends string>({
    value,
    mode,
    setMode,
    title,
    desc,
    tone,
    children,
}: {
    value: T
    mode: T
    setMode: (m: T) => void
    title: string
    desc: string
    tone?: 'warn'
    children?: React.ReactNode
}) {
    const selected = mode === value
    return (
        <div
            className={cn(
                'rounded-lg border px-3.5 py-3 transition-colors',
                selected ? 'border-primary/60 bg-primary/[0.04]' : 'border-border hover:border-border/80',
            )}
        >
            <label className="flex cursor-pointer items-start gap-2.5">
                <input type="radio" className="mt-1" checked={selected} onChange={() => setMode(value)} />
                <span className="min-w-0 flex-1">
                    <span className="block text-[13.5px] font-medium">
                        {title}
                        {tone === 'warn' && (
                            <Badge variant="warning" className="ml-2 text-[10.5px]">
                                affects other subscribers
                            </Badge>
                        )}
                    </span>
                    <span className="mt-0.5 block text-[12.5px] leading-relaxed text-muted-foreground">{desc}</span>
                </span>
            </label>
            {selected && children && <div className="pl-[26px]">{children}</div>}
        </div>
    )
}

/** The as-it-stands / take-it-over question, asked only once a publication is picked.
 *
 * Nested rather than promoted: it is a question about one publication, and
 * there is nothing to ask until the reader has said which. */
function SubChoice({
    checked,
    onSelect,
    title,
    desc,
    tone,
}: {
    checked: boolean
    onSelect: () => void
    title: string
    desc: string
    tone?: 'warn'
}) {
    return (
        <label
            className={cn(
                'flex cursor-pointer items-start gap-2.5 rounded-md border px-2.5 py-2 transition-colors',
                checked ? 'border-primary/50 bg-background' : 'border-transparent hover:border-border/70',
            )}
        >
            <input type="radio" className="mt-0.5" checked={checked} onChange={onSelect} />
            <span className="min-w-0 flex-1">
                <span className="block text-[12.5px] font-medium">
                    {title}
                    {tone === 'warn' && (
                        <Badge variant="warning" className="ml-2 text-[10.5px]">
                            affects other subscribers
                        </Badge>
                    )}
                </span>
                <span className="mt-0.5 block text-[12px] leading-relaxed text-muted-foreground">{desc}</span>
            </span>
        </label>
    )
}

/** Which publication this replica speaks for — asked once, before anything is narrowed.
 *
 * The primary may already carry publications that have nothing to do with this
 * install. Narrowing one means dropping and recreating it, so adopting the
 * wrong one does not misconfigure this replica; it cuts off whoever was using
 * that publication.
 *
 * Two answers at the top — use one of these, or make one of mine — with the
 * dangerous half of the first one asked underneath it, once there is a
 * publication for the question to be about. */
function PublicationChooser({
    base,
    rows,
    suggested,
    onChosen,
}: {
    base: string
    rows: PubRow[]
    suggested: string | null
    onChosen: () => void
}) {
    const [branch, setBranch] = useState<PubBranch>(rows.length ? 'existing' : 'new')
    // Which of the two things "use an existing one" can mean. Kept separate
    // from the branch so that leaving and returning to it does not silently
    // re-arm a takeover the reader had already declined.
    const [takeOver, setTakeOver] = useState(false)
    const mode: PubMode = branch === 'new' ? 'create' : takeOver ? 'adopt' : 'reuse'
    const [picked, setPicked] = useState<string>(rows[0]?.name || '')
    // Filled in with a name nothing on the primary is using — the proposed one,
    // or the next _v2/_v3 after it. Still editable: it is a starting point, not
    // a decision, and the create refuses a taken name anyway.
    const [newName, setNewName] = useState<string>(suggested || '')
    const [busy, setBusy] = useState(false)
    const [err, setErr] = useState<string | null>(null)

    const submit = async () => {
        setBusy(true)
        setErr(null)
        try {
            const name = mode === 'create' ? newName.trim() : picked
            const r = await fetch(`${base}/replication/publication`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, mode }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            onChosen()
        } catch (e: any) {
            setErr(String(e?.message || e))
        } finally {
            setBusy(false)
        }
    }

    return (
        <div className="rounded-xl border border-border bg-card p-5">
            <h2 className="text-[15px] font-semibold">Which publication should this replica use?</h2>
            <p className="mt-1 max-w-[68ch] text-[13px] text-muted-foreground">
                Narrowing a publication replaces it, so nothing is touched until you say which is yours.
            </p>

            {rows.length > 0 && (
                <div className="mt-4 overflow-x-auto">
                    <table className="w-full text-[12.5px]">
                        <thead className="text-muted-foreground">
                            <tr className="border-b border-border/60">
                                <th className="px-1 py-1.5 text-left font-normal">publication</th>
                                <th className="px-1 py-1.5 text-right font-normal tabular-nums">tables</th>
                            </tr>
                        </thead>
                        <tbody className="font-mono">
                            {rows.map((r) => (
                                <tr key={r.name} className="border-b border-border/30 last:border-0">
                                    <td className="px-1 py-1.5">{r.name}</td>
                                    <td className="px-1 py-1.5 text-right tabular-nums">
                                        {r.all_tables ? 'all' : r.table_count}
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}

            <div className="mt-4 grid gap-2">
                {rows.length > 0 && (
                    <ChoiceCard
                        value="existing"
                        mode={branch}
                        setMode={setBranch}
                        title="Use one that is already here"
                        desc={`${rows.length} on this primary. Pick which, then how.`}
                    >
                        <select
                            className="mt-2 w-full rounded-md border border-border bg-background px-2 py-1.5 font-mono text-[12.5px]"
                            value={picked}
                            onChange={(e) => setPicked(e.target.value)}
                        >
                            {rows.map((r) => (
                                <option key={r.name} value={r.name}>
                                    {r.name} — {r.all_tables ? 'all tables' : `${r.table_count} tables`}
                                </option>
                            ))}
                        </select>
                        <div className="mt-2 grid gap-1 rounded-md bg-muted/40 p-1">
                            <SubChoice
                                checked={!takeOver}
                                onSelect={() => setTakeOver(false)}
                                title="As it stands"
                                desc="Replicate what it covers. Never rewritten by this install."
                            />
                            <SubChoice
                                checked={takeOver}
                                onSelect={() => setTakeOver(true)}
                                tone="warn"
                                title="Take it over"
                                desc="This install may change what it covers — including for any other replica using it."
                            />
                        </div>
                    </ChoiceCard>
                )}
                <ChoiceCard
                    value="new"
                    mode={branch}
                    setMode={setBranch}
                    title="Create a new one"
                    desc="Starts empty, filled from what you pick next. Existing publications untouched."
                >
                    <Input
                        className="mt-2 h-8 font-mono text-[12.5px]"
                        placeholder="snaplicator_publication"
                        value={newName}
                        onChange={(e) => setNewName(e.target.value)}
                    />
                </ChoiceCard>
            </div>

            {err && <p className="mt-3 text-[12.5px] text-destructive">{err}</p>}

            <div className="mt-4 flex items-center gap-2">
                <Button
                    size="sm"
                    onClick={submit}
                    disabled={busy || (mode === 'create' ? !newName.trim() : !picked)}
                >
                    {busy ? 'Saving…' : 'Continue'}
                </Button>
                <span className="text-[12px] text-muted-foreground">
                    Changeable later in Config.
                </span>
            </div>
        </div>
    )
}

function Secret({ value }: { value: string }) {
    const [shown, setShown] = useState(false)
    if (!value) return <span className="text-muted-foreground">—</span>
    return (
        <span className="inline-flex items-center gap-1.5">
            <span className="font-mono">{shown ? value : '•'.repeat(Math.min(12, value.length))}</span>
            <button
                onClick={() => setShown((s) => !s)}
                className="text-muted-foreground transition-colors hover:text-foreground"
                title={shown ? 'Hide' : 'Show'}
            >
                {shown ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
            </button>
        </span>
    )
}

export function ReplicationTables() {
    const [tables, setTables] = useState<TableInfo[]>([])
    const [info, setInfo] = useState<ConnInfo | null>(null)
    const [fdw, setFdw] = useState<FdwState | null>(null)
    const [loading, setLoading] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [message, setMessage] = useState<string | null>(null)

    const [search, setSearch] = useState('')
    const [filter, setFilter] = useState<FilterTab>('all')
    const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
    // What the user was trying to make live when they found out they could
    // not — kept so the press can be honoured once the account exists, rather
    // than leaving them to find the row again.
    const [liveDialog, setLiveDialog] = useState<{ open: boolean; target: string[] | null }>({ open: false, target: null })

    // Only the changes are held, never the whole answer: the answer is what
    // the publisher already says, and everything starts included because
    // that is what a fresh publication does. This page is where things are
    // taken out, so an empty map means "leave it exactly as it is".
    const [overrides, setOverrides] = useState<Map<string, TableMode>>(new Map())
    // Whether each schema keeps taking new tables. Separate from the table
    // modes because it is a separate question: what happens to tables that do
    // not exist yet cannot be read off the ones that do.
    const [autoSchemas, setAutoSchemas] = useState<Set<string>>(new Set())
    // The schemas someone switched off. Following is the default, so this —
    // not its complement — is what gets recorded and what has to be read back.
    const [offSchemas, setOffSchemas] = useState<Set<string>>(new Set())
    const [autoOverrides, setAutoOverrides] = useState<Map<string, boolean>>(new Map())
    // Whether a publication is already on the primary, and how much of it it
    // covers. Before the first copy this is the difference between choosing
    // and confirming, and the two deserve different words.
    const [selection, setSelection] = useState<{ exists: boolean; all_tables: boolean; count: number; available: number } | null>(null)

    const [pubs, setPubs] = useState<{ chosen: boolean; ours: boolean; proposed: string | null; suggested: string | null; publications: PubRow[] } | null>(null)
    // "Use one as it stands" is a promise never to rewrite that publication,
    // and the server keeps it: every edit comes back 409. So the page must
    // stop offering edits — and must offer a way to change the answer, since
    // the question is otherwise asked exactly once and never again.
    const readOnlyPub = !!pubs?.chosen && !pubs.ours
    const lockedReason = readOnlyPub
        ? `${info?.publication_name || 'This publication'} is used as it stands — this install never rewrites it. Take it over, or create a new one, to choose what is replicated.`
        : undefined
    const [reopenChooser, setReopenChooser] = useState(false)
    // null until known, so the question never flashes on a settled install.
    const [bootstrapped, setBootstrapped] = useState<boolean | null>(null)
    // Whether what is selected will fit in the pool. Two answers: `fits` is a
    // fact about the disk today and blocks the copy; `comfortable` is a
    // forecast about the snapshots and clones after it, and only gets said.
    const [capacity, setCapacity] = useState<{
        payload_bytes: number; free_bytes: number | null
        fits: boolean | null; comfortable: boolean | null
    } | null>(null)

    const [actionLoading, setActionLoading] = useState(false)
    const [confirmOpen, setConfirmOpen] = useState(false)
    const [refreshLoading, setRefreshLoading] = useState(false)

    const [fdwSet, setFdwSet] = useState<Set<string>>(new Set())
    const [syncEvents, setSyncEvents] = useState<any[]>([])

    const api = import.meta.env.VITE_API_BASE_URL || ''
    const base = api ? api : '/api'

    const loadTables = () => {
        setLoading(true)
        setError(null)
        fetch(`${base}/replication/tables`)
            .then((r) => (r.ok ? r.json() : Promise.reject(r)))
            .then((data: TableInfo[]) => {
                setTables(data)
                setOverrides(new Map())
                // Everything open is unreadable past a handful of schemas, and
                // everything shut hides the only thing the page is for. Open
                // what fits on a screen; shut the rest.
                const bySchema = new Set(data.map((t) => t.schema))
                setCollapsed(bySchema.size > 4 || data.length > 60 ? bySchema : new Set())
            })
            .catch(async (e) => {
                const text = e?.status ? `${e.status} ${await e.text()}` : String(e)
                setError(text)
            })
            .finally(() => setLoading(false))
    }

    const loadInfo = () => {
        fetch(`${base}/replication/info`)
            .then((r) => (r.ok ? r.json() : null))
            .then((data) => { if (data) setInfo(data) })
            .catch(() => {})
    }

    const loadPublications = () => {
        fetch(`${base}/replication/publications`)
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => { if (d) setPubs(d) })
            .catch(() => {})
    }

    const loadSelection = () => {
        fetch(`${base}/replication/selection`)
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => {
                if (!d) return
                setAutoSchemas(new Set<string>(d.auto_schemas || []))
                setOffSchemas(new Set<string>(d.off_schemas || []))
                setAutoOverrides(new Map())
                setSelection({
                    exists: !!d.exists,
                    all_tables: !!d.all_tables,
                    count: Array.isArray(d.tables) ? d.tables.length : 0,
                    available: Array.isArray(d.available) ? d.available.length : 0,
                })
            })
            .catch(() => {})
    }

    const loadFdw = () => {
        fetch(`${base}/replication/fdw`)
            .then((r) => (r.ok ? r.json() : null))
            .then((data: FdwState | null) => {
                if (!data) return
                setFdw(data)
                const next = new Set<string>()
                for (const ft of (data.live_foreign_tables || [])) next.add(`${ft.schema}.${ft.table}`)
                setFdwSet(next)
            })
            .catch(() => {})
    }

    const loadSyncLog = () => {
        fetch(`${base}/replication/sync-log?limit=50`)
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => { if (d && d.events) setSyncEvents(d.events) })
            .catch(() => {})
    }

    // Whether the first copy has run. The publication question belongs to the
    // install and to nothing after it: once tables have been copied, changing
    // which publication the replica speaks for is not a setting to offer in
    // passing — it is a re-bootstrap.
    const loadBootstrap = () => {
        fetch(`${base}/replication/bootstrap`)
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => { if (d) setBootstrapped(d.state === 'done') })
            .catch(() => {})
    }

    const loadCapacity = () => {
        fetch(`${base}/replication/capacity`)
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => setCapacity(d))
            .catch(() => {})
    }

    useEffect(() => {
        loadTables()
        loadCapacity()
        loadInfo()
        loadFdw()
        loadSelection()
        loadPublications()
        loadBootstrap()
        loadSyncLog()
        const id = setInterval(loadSyncLog, 15000)
        return () => clearInterval(id)
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])

    // FDW needs a role on the primary to read as. Without one the option is
    // not a choice the user has, so it is shown as unavailable with the
    // reason rather than offered and then refused by the server.
    // A login is what decides this, not the generated config: the server is
    // built from the login, so asking about the server answers a moment later
    // than the user acts.
    const fdwReady = !!(fdw?.credentials?.configured || fdw?.server?.options?.host)

    // Only once the answer is known: while the selection is still loading,
    // behave as though a publication exists, so a real one never flashes as
    // "everything included" before its actual contents arrive.
    const noPublication = selection ? !selection.exists : false

    const currentMode = (t: TableInfo): TableMode => tableMode(t, fdwSet, noPublication)
    const modeOf = (t: TableInfo): TableMode => overrides.get(`${t.schema}.${t.table}`) ?? currentMode(t)

    const setMode = (fqns: string[], mode: TableMode) =>
        setOverrides((prev) => {
            const next = new Map(prev)
            for (const fqn of fqns) {
                const t = tables.find((x) => `${x.schema}.${x.table}` === fqn)
                if (!t) continue
                if (currentMode(t) === mode) next.delete(fqn)
                else next.set(fqn, mode)
            }
            return next
        })

    // A schema being replicated keeps taking its new tables — that is what the
    // capture trigger does, and it needs nobody to have said so. Only the
    // schemas someone switched off are recorded, so the answer for every other
    // schema is read off what is being replicated right now. That includes the
    // schema created after the last save, and the very first selection, where
    // there is nothing recorded to read at all.
    const replicatedSchemas = useMemo(() => {
        const out = new Set<string>()
        for (const t of tables) if (modeOf(t) === 'replicated') out.add(t.schema)
        return out
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [tables, overrides, fdwSet, noPublication])

    const autoBase = (schema: string) =>
        autoSchemas.has(schema) || (!offSchemas.has(schema) && replicatedSchemas.has(schema))
    const autoOf = (schema: string) => autoOverrides.get(schema) ?? autoBase(schema)
    const setAuto = (schema: string, on: boolean) =>
        setAutoOverrides((prev) => {
            const next = new Map(prev)
            if (autoBase(schema) === on) next.delete(schema)
            else next.set(schema, on)
            return next
        })

    const filtered = useMemo(() => {
        let list = tables
        if (filter !== 'all') list = list.filter((t) => modeOf(t) === filter)
        if (search.trim()) {
            const q = search.trim().toLowerCase()
            list = list.filter((t) => `${t.schema}.${t.table}`.toLowerCase().includes(q))
        }
        return list
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [tables, filter, search, fdwSet, overrides, noPublication])

    // Grouped by schema, which is the shape the database has and the shape the
    // decision has: whole schemas are what people actually keep or drop, and
    // individual tables are the exception inside one.
    const groups = useMemo(() => {
        const map = new Map<string, TableInfo[]>()
        for (const t of filtered) {
            const arr = map.get(t.schema)
            if (arr) arr.push(t)
            else map.set(t.schema, [t])
        }
        return Array.from(map.entries())
            .map(([schema, items]) => {
                let replicated = 0, fdwCount = 0, excluded = 0, rows = 0, changed = 0, missingOnSub = 0
                for (const t of items) {
                    const m = modeOf(t)
                    if (m === 'replicated') replicated++
                    else if (m === 'fdw') fdwCount++
                    else excluded++
                    rows += t.estimated_rows
                    if (m !== currentMode(t)) changed++
                    if (t.in_publication && !t.in_subscriber) missingOnSub++
                }
                return {
                    schema,
                    items: items.slice().sort((a, b) => a.table.localeCompare(b.table)),
                    replicated, fdwCount, excluded, rows, changed, missingOnSub,
                }
            })
            .sort((a, b) => a.schema.localeCompare(b.schema))
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [filtered, fdwSet, overrides, noPublication])

    const stats = useMemo(() => {
        let replicated = 0, fdwCount = 0, none = 0
        for (const t of tables) {
            const m = modeOf(t)
            if (m === 'replicated') replicated++
            else if (m === 'fdw') fdwCount++
            else none++
        }
        return { total: tables.length, replicated, fdw: fdwCount, none, schemas: new Set(tables.map((t) => t.schema)).size }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [tables, fdwSet, overrides, noPublication])

    // A search is a request to see what matched, so matching schemas open
    // themselves and the collapse state is left alone underneath.
    const searching = search.trim().length > 0
    const isOpen = (schema: string) => searching || !collapsed.has(schema)

    const toggleSchema = (schema: string) =>
        setCollapsed((prev) => {
            const next = new Set(prev)
            if (next.has(schema)) next.delete(schema)
            else next.add(schema)
            return next
        })

    const pendingAuto = useMemo(() => {
        const out: { schema: string; to: boolean }[] = []
        for (const [schema, to] of autoOverrides) {
            if (autoBase(schema) !== to) out.push({ schema, to })
        }
        return out.sort((a, b) => a.schema.localeCompare(b.schema))
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [autoOverrides, autoSchemas, offSchemas, replicatedSchemas])

    const pending = useMemo(() => {
        const out: { fqn: string; from: TableMode; to: TableMode }[] = []
        for (const [fqn, to] of overrides) {
            const t = tables.find((x) => `${x.schema}.${x.table}` === fqn)
            if (!t) continue
            const from = currentMode(t)
            if (from !== to) out.push({ fqn, from, to })
        }
        return out.sort((a, b) => a.fqn.localeCompare(b.fqn))
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [overrides, tables, fdwSet, noPublication])

    const apply = async (): Promise<boolean> => {
        setActionLoading(true)
        setError(null)
        setMessage(null)
        try {
            const wantFdw = new Set<string>()
            const wantReplicate: string[] = []
            for (const t of tables) {
                const fqn = `${t.schema}.${t.table}`
                const m = modeOf(t)
                if (m === 'fdw') wantFdw.add(fqn)
                else if (m === 'replicated') wantReplicate.push(fqn)
            }
            // Sent as chosen, not inferred. Whether a schema keeps taking new
            // tables is its own answer: a schema with two tables left out can
            // still want the next one, and a complete schema can still want to
            // stay exactly as it is.
            const wantAuto = Array.from(new Set(tables.map((t) => t.schema))).filter(autoOf)

            // Unmap first, publish second, map last: a table cannot be both a
            // foreign table and a published one, so each change passes through
            // the state where it is neither.
            const leavingFdw = Array.from(fdwSet).filter((f) => !wantFdw.has(f))
            if (leavingFdw.length) {
                const r = await fetch(`${base}/replication/fdw/tables`, {
                    method: 'DELETE',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ tables: leavingFdw.map(splitFqn) }),
                })
                if (!r.ok) throw new Error(`unmapping FDW: ${r.status} ${await r.text()}`)
            }

            const sel = await fetch(`${base}/replication/selection`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tables: wantReplicate, auto_schemas: wantAuto }),
            })
            if (!sel.ok) throw new Error(`publication: ${sel.status} ${await sel.text()}`)
            const selRes = await sel.json()

            const joiningFdw = Array.from(wantFdw).filter((f) => !fdwSet.has(f))
            if (joiningFdw.length) {
                const r = await fetch(`${base}/replication/fdw/tables`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ tables: joiningFdw.map(splitFqn) }),
                })
                if (!r.ok) throw new Error(`mapping FDW: ${r.status} ${await r.text()}`)
            }

            setMessage(
                `Publication now ${selRes.form} — ${selRes.count} table(s)` +
                (selRes.subscription_refreshed ? ', subscription refreshed' : ''),
            )
            setConfirmOpen(false)
            loadTables()
            loadSelection()
            loadFdw()
            return true
        } catch (e: any) {
            setError(String(e?.message || e))
            return false
        } finally {
            setActionLoading(false)
        }
    }

    // What "start" has to mean: the copy runs against what is on screen. A
    // staged exclusion that the copy ignored was the difference between the
    // page's promise and the replica's contents, and the copy is the point
    // where that stops being fixable without starting over.
    const commitThenStart = async (): Promise<boolean> => {
        if (!pending.length && !pendingAuto.length) return true
        return apply()
    }

    const onRefresh = async () => {
        setRefreshLoading(true)
        setError(null)
        setMessage(null)
        try {
            const r = await fetch(`${base}/replication/refresh`, { method: 'POST' })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            setMessage('Subscription refreshed')
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setRefreshLoading(false)
        }
    }

    const openLive = (target: string[] | null) => setLiveDialog({ open: true, target })

    // The account is in place, so the press that asked for it can go through.
    // Only the FDW state is reloaded: saving a login maps no tables, and
    // reloading the list would clear the pending change this just made.
    const onFdwConfigured = () => {
        const target = liveDialog.target
        setLiveDialog({ open: false, target: null })
        loadFdw()
        if (target?.length) setMode(target, 'fdw')
    }

    return (
        <div className={cn('mx-auto max-w-2xl animate-page-in px-6 pt-6', (pending.length || pendingAuto.length) ? 'pb-32' : 'pb-20')}>
            <div className="mb-4 flex items-center justify-between gap-4 border-b border-border pb-4">
                <div className="flex items-center gap-3">
                    <Button asChild size="sm">
                        <Link to="/config">&larr; Back</Link>
                    </Button>
                    <h1 className="text-base font-semibold tracking-tight">Replication</h1>
                </div>
                <div className="flex items-center gap-2">
                    <Button onClick={onRefresh} disabled={refreshLoading} size="sm">
                        {refreshLoading ? 'Refreshing…' : 'Refresh subscription'}
                    </Button>
                </div>
            </div>

            {/* Asked before anything else on this page, and only when there is
                something to get wrong: publications already on the primary
                that this install did not create. Answering it is what gives
                the rest of the page permission to narrow anything.

                And only before the first copy. After it the replica is already
                reading from a publication, so the question is no longer which
                one to adopt — it is whether to throw away what was copied. */}
            {pubs && (!pubs.chosen || reopenChooser) && pubs.publications.length > 0 && bootstrapped === false && (
                <PublicationChooser
                    base={base}
                    rows={pubs.publications}
                    suggested={pubs.suggested ?? pubs.proposed}
                    onChosen={() => {
                        setReopenChooser(false)
                        loadPublications()
                        loadSelection()
                        loadTables()
                        loadInfo()
                    }}
                />
            )}

            {/* Said once, above the tables, because every control below it is
                inert and a page of quietly dead buttons is worse than the
                error it replaced. */}
            {readOnlyPub && !reopenChooser && bootstrapped === false && (
                <div className="rounded-xl border border-border bg-card px-5 py-4">
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                        <span className="text-[13px]">
                            <span className="font-mono">{info?.publication_name}</span> is used as it
                            stands — <span className="text-muted-foreground">this install never rewrites it.</span>
                        </span>
                        <Button size="sm" variant="ghost" onClick={() => setReopenChooser(true)}>
                            Choose differently
                        </Button>
                    </div>
                    <p className="mt-1 text-[12.5px] text-muted-foreground">
                        What it covers is what gets replicated. To pick tables yourself, take it over
                        or create a new one.
                    </p>
                </div>
            )}

            {/* Before the first copy this page is the whole install: the
                choice is made here, and the button that acts on it belongs
                next to the choice rather than a page away. */}
            <BootstrapGate
                onDone={() => { loadTables(); loadBootstrap() }}
                title={selection?.exists ? 'A publication already exists — start from it?' : undefined}
                onBeforeStart={commitThenStart}
                startLabel={
                    pending.length + pendingAuto.length > 0
                        ? `Apply ${pending.length + pendingAuto.length} change${pending.length + pendingAuto.length === 1 ? '' : 's'} and start the copy`
                        : selection?.exists
                            ? 'Start the copy with this selection'
                            : undefined
                }
                hint={
                    capacity && capacity.fits === false ? (
                        <span className="text-destructive">
                            The selection is {gib(capacity.payload_bytes)} and the pool has{' '}
                            {gib(capacity.free_bytes)} free — the copy cannot finish.
                        </span>
                    ) : capacity && capacity.comfortable === false ? (
                        <>
                            {gib(capacity.payload_bytes)} selected, {gib(capacity.free_bytes)} free —
                            enough to copy, tight for snapshots and clones.
                        </>
                    ) : selection?.exists ? (
                        <>
                            <span className="font-mono text-foreground">{info?.publication_name || 'A publication'}</span>{' '}
                            covers{' '}
                            <span className="text-foreground">
                                {selection.all_tables
                                    ? `every table (${selection.available})`
                                    : `${selection.count} of ${selection.available} tables`}
                            </span>
                            .
                            <PublicationCoverage tables={tables} />
                        </>
                    ) : null
                }
            />

            {/* What is happening to this database, in one line. */}
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[13px]">
                <span><span className="font-semibold text-success">{stats.replicated}</span> <span className="text-muted-foreground">replicated</span></span>
                {/* Also the way back in once it is set up — the switch stops
                    asking the moment the account exists, so the count is what
                    is left to open it with. */}
                <button
                    onClick={() => openLive(null)}
                    className="group flex items-center gap-1 transition-colors"
                    title="Live reads (FDW)"
                >
                    <span className="font-semibold text-purple">{stats.fdw}</span>
                    <span className="text-muted-foreground decoration-muted-foreground/40 underline-offset-4 group-hover:text-foreground group-hover:underline">
                        live (FDW)
                    </span>
                    {!fdwReady && <span className="text-muted-foreground/50">· not set up</span>}
                </button>
                <span><span className="font-semibold">{stats.none}</span> <span className="text-muted-foreground">excluded</span></span>
                <span className="text-muted-foreground">·</span>
                <span className="text-muted-foreground">{stats.total} tables in {stats.schemas} schemas</span>
            </div>

            <Disclosure
                title="Connection"
                summary={info ? `${info.publisher.host}:${info.publisher.port}/${info.publisher.db} → ${info.subscriber.container}` : undefined}
            >
                {info && (
                    <div className="grid gap-6 sm:grid-cols-2">
                        <div>
                            <div className="mb-2 text-[13px] font-semibold text-info">Publisher</div>
                            <dl className="grid grid-cols-[92px_1fr] gap-y-1 text-[13px]">
                                <dt className="text-muted-foreground">Host</dt><dd className="font-mono">{info.publisher.host}:{info.publisher.port}</dd>
                                <dt className="text-muted-foreground">Database</dt><dd className="font-mono">{info.publisher.db}</dd>
                                <dt className="text-muted-foreground">User</dt><dd className="font-mono">{info.publisher.user}</dd>
                                <dt className="text-muted-foreground">Password</dt><dd><Secret value={info.publisher.password} /></dd>
                                <dt className="text-muted-foreground">Publication</dt><dd className="font-mono">{info.publication_name}</dd>
                            </dl>
                        </div>
                        <div>
                            <div className="mb-2 text-[13px] font-semibold text-success">Subscriber</div>
                            <dl className="grid grid-cols-[92px_1fr] gap-y-1 text-[13px]">
                                <dt className="text-muted-foreground">Container</dt><dd className="font-mono">{info.subscriber.container}</dd>
                                <dt className="text-muted-foreground">Host</dt><dd className="font-mono">{info.subscriber.host}:{info.subscriber.port}</dd>
                                <dt className="text-muted-foreground">Database</dt><dd className="font-mono">{info.subscriber.db}</dd>
                                <dt className="text-muted-foreground">User</dt><dd className="font-mono">{info.subscriber.user}</dd>
                                <dt className="text-muted-foreground">Password</dt><dd><Secret value={info.subscriber.password} /></dd>
                                <dt className="text-muted-foreground">Subscription</dt><dd className="font-mono">{info.subscription_name}</dd>
                            </dl>
                        </div>
                    </div>
                )}
            </Disclosure>

            {message && <p className="mt-3 text-[13px] text-success">{message}</p>}
            {error && <p className="mt-3 text-[13px] text-destructive">{error}</p>}

            <div className="mt-4 flex flex-wrap items-center gap-2">
                <div className="relative max-w-96 flex-1">
                    <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                    <Input
                        placeholder="Search schema.table…"
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                        className="pl-8"
                    />
                </div>
                <div className="flex gap-1">
                    {([
                        ['all', `All (${stats.total})`],
                        ['replicated', `Replicated (${stats.replicated})`],
                        ['fdw', `Live (${stats.fdw})`],
                        ['none', `Excluded (${stats.none})`],
                    ] as [FilterTab, string][]).map(([key, label]) => (
                        <Button key={key} size="sm" variant={filter === key ? 'primary' : 'ghost'} onClick={() => setFilter(key)}>
                            {label}
                        </Button>
                    ))}
                </div>
                {groups.length > 0 && (
                    <Button
                        size="sm"
                        variant="ghost"
                        className="ml-auto"
                        onClick={() => setCollapsed(collapsed.size ? new Set() : new Set(groups.map((g) => g.schema)))}
                    >
                        {collapsed.size ? 'Expand all' : 'Collapse all'}
                    </Button>
                )}
            </div>

            {/* A schema and its tables, as an outline rather than a table.
                Rules between every row draw a cell around each one, which is
                what a grid is for — but there is only one column of interest
                here, and the thing worth seeing is which tables hang off which
                schema. Indentation and a single guide line say that; twelve
                horizontal rules say nothing and cost the eye a stop each. */}
            <div className="mt-3">
                {loading && groups.length === 0 && <div className="py-10 text-center text-[13px] text-muted-foreground">Loading…</div>}
                {!loading && groups.length === 0 && (
                    <div className="py-10 text-center text-[13px] text-muted-foreground">
                        {tables.length === 0 ? 'No tables on the publisher.' : 'Nothing matches this filter.'}
                    </div>
                )}

                {groups.map((g) => {
                    const fqns = g.items.map((t) => `${t.schema}.${t.table}`)
                    const open = isOpen(g.schema)
                    const schemaMode: TableMode | null =
                        g.replicated === g.items.length ? 'replicated'
                            : g.fdwCount === g.items.length ? 'fdw'
                                : g.excluded === g.items.length ? 'none'
                                    : null
                    return (
                        <div key={g.schema} className="mb-2">
                            {/* Header and tables share one grid, so the state
                                and the row counts line up as columns across
                                both — the indent lives inside the first cell
                                rather than shifting everything after it. */}
                            <div
                                onClick={() => toggleSchema(g.schema)}
                                className={cn(
                                    'group grid cursor-pointer items-center gap-2 rounded-md py-1.5 pr-2 transition-colors hover:bg-white/[0.035]',
                                    ROW_GRID,
                                )}
                            >
                                <div className="flex min-w-0 items-center gap-1.5">
                                    {open
                                        ? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground/70" />
                                        : <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground/70" />}
                                    <span className={cn('truncate font-mono text-[13px] font-semibold', schemaMode === 'none' && 'text-muted-foreground')}>
                                        {g.schema}
                                    </span>
                                    <span className="shrink-0 text-xs text-muted-foreground/60">
                                        {g.items.length} {g.items.length === 1 ? 'table' : 'tables'}
                                    </span>
                                    {g.changed > 0 && <Badge variant="info">{g.changed} changed</Badge>}
                                    {schemaMode === null && (
                                        <span className="shrink-0 text-xs text-muted-foreground/60">
                                            · {g.replicated} replicated{g.fdwCount > 0 && `, ${g.fdwCount} live`}
                                        </span>
                                    )}
                                </div>
                                <ModeSwitch
                                    value={schemaMode}
                                    fdwReady={fdwReady}
                                    locked={lockedReason}
                                    onChange={(m) => setMode(fqns, m)}
                                    onNeedsFdw={() => openLive(fqns)}
                                />
                                <span className="text-right font-mono text-xs text-muted-foreground">{formatRows(g.rows)}</span>
                            </div>

                            {/* One guide line, drawn beside the children rather
                                than by indenting them — the one mark that says
                                where this schema's tables start and stop. */}
                            {open && (
                                <div className="relative">
                                    <span className="pointer-events-none absolute bottom-1.5 left-[15px] top-0 w-px bg-white/[0.14]" />
                                    {g.items.map((t) => {
                                        const fqn = `${t.schema}.${t.table}`
                                        const m = modeOf(t)
                                        const changed = m !== currentMode(t)
                                        return (
                                            <div
                                                key={fqn}
                                                className={cn(
                                                    'group grid items-center gap-2 rounded-md py-1 pr-2 text-[13px] transition-colors hover:bg-white/[0.035]',
                                                    ROW_GRID,
                                                    changed && 'bg-info/[0.08]',
                                                )}
                                            >
                                                <div className="flex min-w-0 items-center gap-1.5 pl-[26px]">
                                                    <span className={cn('truncate font-mono', m === 'none' && 'text-muted-foreground/50')}>
                                                        {t.table}
                                                    </span>
                                                    {t.in_publication && !t.in_subscriber && (
                                                        <span
                                                            className="shrink-0 text-warning"
                                                            title="In the publication, but not on the subscriber yet"
                                                        >
                                                            ·
                                                        </span>
                                                    )}
                                                </div>
                                                <ModeSwitch
                                                    value={m}
                                                    fdwReady={fdwReady}
                                                    locked={lockedReason}
                                                    onChange={(next) => setMode([fqn], next)}
                                                    onNeedsFdw={() => openLive([fqn])}
                                                />
                                                <span className="text-right font-mono text-xs text-muted-foreground">{formatRows(t.estimated_rows)}</span>
                                            </div>
                                        )
                                    })}
                                    <FollowRow on={autoOf(g.schema)} locked={lockedReason} onChange={(v) => setAuto(g.schema, v)} />
                                </div>
                            )}
                        </div>
                    )
                })}
            </div>

            <Disclosure title="Auto-sync activity" summary={`${syncEvents.length} recent · refreshes every 15s`}>
                {syncEvents.length === 0 ? (
                    <div className="text-[13px] text-muted-foreground">Nothing recorded yet.</div>
                ) : (
                    <div className="max-h-72 overflow-auto">
                        {syncEvents.map((e, i) => {
                            const f = fmtSync(e)
                            return (
                                <div key={i} className="border-b border-border py-2 last:border-b-0">
                                    <div className="mb-1 flex items-center gap-2">
                                        <Badge variant={TONE_VARIANT[f.tone]}>{f.label}</Badge>
                                        <span className="ml-auto text-xs text-muted-foreground" title={e.ts}>{new Date(e.ts).toLocaleString()}</span>
                                    </div>
                                    <div className="text-[13px] leading-relaxed">
                                        {f.lines.map((ln, j) => (
                                            <div key={j} className={cn(j !== 0 && 'text-muted-foreground', ln.startsWith('·') && 'pl-2.5')}>{ln}</div>
                                        ))}
                                    </div>
                                </div>
                            )
                        })}
                    </div>
                )}
            </Disclosure>

            {/* Nothing is sent until it is asked for: the switches are a draft
                of the publication, and this is where the draft becomes it. */}
            {(pending.length > 0 || pendingAuto.length > 0) && (
                <div className="fixed inset-x-0 bottom-0 z-20 border-t border-border-strong bg-background/95 backdrop-blur">
                    <div className="mx-auto flex max-w-2xl flex-wrap items-center gap-2 px-6 py-3">
                        <span className="text-[13px]">
                            <span className="font-semibold">{pending.length + pendingAuto.length}</span>{' '}
                            <span className="text-muted-foreground">
                                change{pending.length + pendingAuto.length === 1 ? '' : 's'} not applied
                            </span>
                        </span>
                        <Button size="sm" variant="ghost" onClick={() => { setOverrides(new Map()); setAutoOverrides(new Map()) }} disabled={actionLoading}>
                            Discard
                        </Button>
                        <Button size="sm" variant="primary" onClick={() => setConfirmOpen(true)} disabled={actionLoading}>
                            Apply
                        </Button>
                        <span className="text-xs text-muted-foreground">
                            {pending.filter((p) => p.to === 'none').length} excluded ·{' '}
                            {pending.filter((p) => p.to === 'fdw').length} live ·{' '}
                            {pending.filter((p) => p.to === 'replicated').length} replicated
                            {pendingAuto.length > 0 && ` · ${pendingAuto.length} schema follow rule${pendingAuto.length === 1 ? '' : 's'}`}
                        </span>
                    </div>
                </div>
            )}

            <LiveDialog
                open={liveDialog.open}
                onOpenChange={(open) => setLiveDialog((s) => ({ open, target: open ? s.target : null }))}
                base={base}
                fdw={fdw}
                fdwReady={fdwReady}
                target={liveDialog.target?.length === 1 ? liveDialog.target[0] : null}
                onConfigured={onFdwConfigured}
            />

            <Dialog open={confirmOpen} onOpenChange={(open) => { if (!open && !actionLoading) setConfirmOpen(false) }}>
                <DialogContent className="max-w-lg">
                    <DialogTitle>
                        Apply {pending.length + pendingAuto.length} change
                        {pending.length + pendingAuto.length === 1 ? '' : 's'}
                    </DialogTitle>
                    <DialogDescription>
                        {tables.some((t) => t.in_subscriber)
                            ? 'Added tables start copying. Removed tables keep the rows they already have.'
                            : 'This decides what the first copy includes.'}
                    </DialogDescription>
                    <div className="my-2 max-h-52 overflow-y-auto rounded-md border border-border bg-secondary p-2 font-mono text-[13px]">
                        {pendingAuto.map((p) => (
                            <div key={`auto:${p.schema}`} className="flex items-center gap-2">
                                <span className="truncate">{p.schema}</span>
                                <span className="ml-auto shrink-0 text-muted-foreground">
                                    new tables → <span className="text-foreground">{p.to ? 'follow' : 'ignore'}</span>
                                </span>
                            </div>
                        ))}
                        {pending.map((p) => (
                            <div key={p.fqn} className="flex items-center gap-2">
                                <span className="truncate">{p.fqn}</span>
                                <span className="ml-auto shrink-0 text-muted-foreground">
                                    {MODE_LABEL[p.from]} → <span className="text-foreground">{MODE_LABEL[p.to]}</span>
                                </span>
                            </div>
                        ))}
                    </div>
                    <DialogFooter>
                        <Button onClick={() => setConfirmOpen(false)} disabled={actionLoading}>Cancel</Button>
                        <Button variant="primary" onClick={apply} disabled={actionLoading}>
                            {actionLoading ? 'Working…' : 'Apply'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </div>
    )
}
