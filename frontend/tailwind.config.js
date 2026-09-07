/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: {
          50: '#f6f7f9', 100: '#eceef2', 200: '#d5dae2', 300: '#b0b9c8',
          400: '#8593a9', 500: '#65748d', 600: '#505d74', 700: '#414b5e',
          800: '#38404f', 900: '#141821', 950: '#0b0e14',
        },
        brand: {
          50: '#eefbf4', 100: '#d6f5e4', 200: '#b0eacd', 300: '#7bd9ae',
          400: '#44c08b', 500: '#1fa571', 600: '#12855b', 700: '#106a4b',
          800: '#11543d', 900: '#0f4634', 950: '#04271d',
        },
        signal: {
          amber: '#f0a92a', red: '#e5484d', blue: '#3b82f6', violet: '#8b5cf6',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      keyframes: {
        'fade-up': { '0%': { opacity: '0', transform: 'translateY(6px)' }, '100%': { opacity: '1', transform: 'none' } },
        'pulse-ring': { '0%': { transform: 'scale(0.9)', opacity: '0.7' }, '70%': { transform: 'scale(1.6)', opacity: '0' }, '100%': { opacity: '0' } },
        'dash': { to: { strokeDashoffset: '0' } },
      },
      animation: {
        'fade-up': 'fade-up 260ms ease-out',
        'pulse-ring': 'pulse-ring 1.8s cubic-bezier(0.4,0,0.6,1) infinite',
        'dash': 'dash 1.4s ease-out forwards',
      },
    },
  },
  plugins: [],
}
