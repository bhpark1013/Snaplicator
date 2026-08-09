/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        background: 'rgb(var(--background) / <alpha-value>)',
        foreground: 'rgb(var(--foreground) / <alpha-value>)',
        card: 'rgb(var(--card) / <alpha-value>)',
        secondary: 'rgb(var(--secondary) / <alpha-value>)',
        accent: 'rgb(var(--accent) / <alpha-value>)',
        muted: {
          DEFAULT: 'rgb(var(--secondary) / <alpha-value>)',
          foreground: 'rgb(var(--muted-foreground) / <alpha-value>)',
        },
        primary: {
          DEFAULT: 'rgb(var(--primary) / <alpha-value>)',
          foreground: '#ffffff',
        },
        destructive: 'rgb(var(--destructive) / <alpha-value>)',
        success: 'rgb(var(--success) / <alpha-value>)',
        warning: 'rgb(var(--warning) / <alpha-value>)',
        info: 'rgb(var(--info) / <alpha-value>)',
        purple: 'rgb(var(--purple) / <alpha-value>)',
        border: 'rgba(255, 255, 255, 0.08)',
        'border-strong': 'rgba(255, 255, 255, 0.15)',
        ring: 'rgb(var(--primary) / <alpha-value>)',
      },
      fontFamily: {
        sans: ['Inter', 'Pretendard', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Apple SD Gothic Neo', 'sans-serif'],
        mono: ['JetBrains Mono', 'ui-monospace', 'SFMono-Regular', 'SF Mono', 'Menlo', 'Consolas', 'monospace'],
      },
      borderRadius: {
        lg: '8px',
        md: '6px',
      },
      keyframes: {
        'page-in': {
          from: { opacity: '0', transform: 'translateY(4px)' },
          to: { opacity: '1', transform: 'none' },
        },
        'toast-in': {
          from: { opacity: '0', transform: 'translateX(8px)' },
          to: { opacity: '1', transform: 'none' },
        },
      },
      animation: {
        // `backwards`, not `both`. A filling animation whose keyframes mention
        // transform leaves the element with a computed transform forever —
        // matrix(1,0,0,1,0,0), an identity, but not `none` — and any transform
        // other than none makes that element the containing block for every
        // position:fixed descendant. The page wrapper carries page-in, so a
        // bar pinned to the bottom of the viewport was instead pinned to the
        // bottom of a 22,000px page: present, correct, and 7,000px below the
        // fold. `backwards` still applies the from-state before the animation
        // starts, so nothing flashes, and drops the fill once it ends.
        'page-in': 'page-in 0.25s ease backwards',
        'toast-in': 'toast-in 0.14s ease-out backwards',
      },
    },
  },
  plugins: [],
}
