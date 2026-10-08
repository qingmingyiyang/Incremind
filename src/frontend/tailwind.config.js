/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        'cr-red': 'var(--red)',
        'cr-red-dark': 'var(--red)',
        'cr-red-medium': 'var(--red)',
        'cr-red-soft': 'var(--sunk)',
        'cr-canvas': 'var(--paper)',
        'cr-paper': 'var(--panel)',
        'cr-line': 'var(--line)',
        'cr-ink': 'var(--ink)',
        'cr-text': 'var(--ink2)',
        'cr-muted': 'var(--muted)',
        'cr-faint': 'var(--muted)',
        'cr-green': 'var(--ink)',
        'cr-warning': 'var(--red)',
        'cr-error': 'var(--red)',
      },
      fontFamily: {
        heading: 'var(--serif)',
        sans: 'var(--sans)',
        mono: 'var(--mono)',
      },
      borderRadius: {
        'panel': 'var(--r-panel)',
        'button': 'var(--r-control)',
        'modal': 'var(--r-panel)',
        'pill': '999px',
        'code': 'var(--r-panel)',
      },
    },
  },
  plugins: [],
};
