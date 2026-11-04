/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './src/pages/**/*.{js,ts,jsx,tsx,mdx}',
    './src/components/**/*.{js,ts,jsx,tsx,mdx}',
    './src/app/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      colors: {
        primary: '#6366f1',
        surface: '#131a2b',
        muted: '#6b7590',
      },
      boxShadow: {
        ring: '0 0 0 3px rgba(34, 211, 238, 0.25)',
      },
    },
  },
  plugins: [],
};
