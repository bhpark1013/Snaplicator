import { cn } from '@/lib/utils'

// Retention in days. 0 means "keep forever" (permanent).
const PRESETS: { label: string; days: number }[] = [
    { label: '1d', days: 1 },
    { label: '3d', days: 3 },
    { label: '7d', days: 7 },
    { label: '14d', days: 14 },
    { label: '30d', days: 30 },
    { label: '∞', days: 0 },
]

export function RetentionSelect({
    value,
    onChange,
    className,
}: {
    value: number
    onChange: (days: number) => void
    className?: string
}) {
    const isPreset = PRESETS.some((p) => p.days === value)
    return (
        <div className={cn('grid gap-1.5', className)}>
            <span className="text-[13px] text-muted-foreground">
                Retention {value === 0 ? '(kept forever)' : `(${value} day${value === 1 ? '' : 's'})`}
            </span>
            <div className="flex flex-wrap items-center gap-1.5">
                {PRESETS.map((p) => (
                    <button
                        key={p.days}
                        type="button"
                        onClick={() => onChange(p.days)}
                        className={cn(
                            'h-8 min-w-[2.5rem] rounded-md border px-2.5 text-[13px] font-medium transition-colors',
                            value === p.days
                                ? 'border-primary bg-primary text-primary-foreground'
                                : 'border-border-strong bg-secondary text-foreground hover:border-primary/60',
                        )}
                        title={p.days === 0 ? 'Keep forever' : `${p.days} days`}
                    >
                        {p.label}
                    </button>
                ))}
                <div className="flex items-center gap-1">
                    <input
                        type="number"
                        min={1}
                        value={isPreset ? '' : value || ''}
                        onChange={(e) => {
                            const n = parseInt(e.target.value, 10)
                            onChange(Number.isFinite(n) && n > 0 ? n : 0)
                        }}
                        placeholder="custom"
                        className={cn(
                            'h-8 w-[5.5rem] rounded-md border bg-secondary px-2.5 text-[13px] text-foreground transition-colors focus-visible:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/25',
                            !isPreset && value > 0 ? 'border-primary' : 'border-border-strong',
                        )}
                    />
                    <span className="text-[12px] text-muted-foreground">days</span>
                </div>
            </div>
        </div>
    )
}
