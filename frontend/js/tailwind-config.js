// Tailwind Play CDN configuration. A classic (non-module) script that must run right after the CDN script,
// so that index.html needs no inline <script> block and the Content-Security-Policy can forbid inline scripts.
tailwind.config = {
  darkMode: 'class',
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'monospace'],
      },
    },
  },
};
