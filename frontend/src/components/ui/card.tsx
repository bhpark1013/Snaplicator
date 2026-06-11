import * as React from 'react'

import { cn } from '@/lib/utils'

const Card = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
    ({ className, ...props }, ref) => (
        <div
            ref={ref}
            className={cn('rounded-lg border border-border bg-card p-4', className)}
            {...props}
        />
    ),
)
Card.displayName = 'Card'

const CardTitle = React.forwardRef<HTMLHeadingElement, React.HTMLAttributes<HTMLHeadingElement>>(
    ({ className, ...props }, ref) => (
        <h2
            ref={ref}
            className={cn('text-[13px] font-semibold tracking-tight text-foreground', className)}
            {...props}
        />
    ),
)
CardTitle.displayName = 'CardTitle'

export { Card, CardTitle }
