import { cn } from '@/lib/utils'

/** A grey placeholder block that pulses while its content loads. */
export function Skeleton({ className, style }: { className?: string; style?: React.CSSProperties }) {
    return <div aria-hidden className={cn('animate-pulse rounded bg-accent', className)} style={style} />
}

// Varying title widths so the placeholder reads as a list, not a grid.
const WIDTHS = [38, 52, 30, 46, 34, 58, 42, 26]

/** Placeholder rows shaped like the app's list rows: a title, a subline,
 *  and `actions` button-sized blocks on the right. */
export function SkeletonRows({
    count = 6,
    actions = 0,
    as = 'div',
    lead = false,
}: {
    count?: number
    actions?: number
    as?: 'div' | 'li'
    lead?: boolean
}) {
    const Row = as
    return (
        <>
            {Array.from({ length: count }, (_, i) => (
                <Row
                    key={`skeleton-${i}`}
                    aria-hidden
                    className="flex animate-pulse items-center gap-3 rounded-md border border-border bg-secondary px-3.5 py-2.5"
                >
                    {lead && <div className="size-5 flex-none rounded bg-accent" />}
                    <div className="grid min-w-0 flex-1 gap-2">
                        <div className="h-3.5 rounded bg-accent" style={{ width: `${WIDTHS[i % WIDTHS.length]}%` }} />
                        <div className="h-3 w-3/5 rounded bg-accent/70" />
                    </div>
                    {actions > 0 && (
                        <div className="ml-auto flex flex-none gap-2">
                            {Array.from({ length: actions }, (_, j) => (
                                <div key={j} className="h-8 w-16 rounded-md bg-accent" />
                            ))}
                        </div>
                    )}
                </Row>
            ))}
        </>
    )
}
