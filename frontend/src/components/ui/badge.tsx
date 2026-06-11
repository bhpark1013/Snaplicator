import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'

import { cn } from '@/lib/utils'

const badgeVariants = cva(
    'inline-flex items-center gap-1.5 rounded-full border px-2 py-px text-[11.5px] font-medium leading-snug',
    {
        variants: {
            variant: {
                neutral: 'border-border-strong text-muted-foreground',
                success: 'border-success/35 bg-success/10 text-success',
                destructive: 'border-destructive/35 bg-destructive/10 text-destructive',
                warning: 'border-warning/35 bg-warning/10 text-warning',
                info: 'border-info/35 bg-info/10 text-info',
                purple: 'border-purple/35 bg-purple/10 text-purple',
            },
        },
        defaultVariants: {
            variant: 'neutral',
        },
    },
)

export interface BadgeProps
    extends React.HTMLAttributes<HTMLSpanElement>,
        VariantProps<typeof badgeVariants> {
    dot?: boolean
}

function Badge({ className, variant, dot = true, children, ...props }: BadgeProps) {
    return (
        <span className={cn(badgeVariants({ variant }), className)} {...props}>
            {dot && <span aria-hidden className="size-1.5 shrink-0 rounded-full bg-current" />}
            {children}
        </span>
    )
}

export { Badge, badgeVariants }
