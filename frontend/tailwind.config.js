/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        brand: {
          50: "#eef5ff",
          100: "#d9e8ff",
          200: "#bcd7ff",
          300: "#8ebeff",
          400: "#599bff",
          500: "#3478f6",
          600: "#245ce0",
          700: "#1e49b4",
          800: "#1c3f8f",
          900: "#1c3971",
          950: "#141f42",
        },
        gain: {
          DEFAULT: "#34d399",
          light: "#6ee7b7",
          dark: "#059669",
          bg: "rgba(52, 211, 153, 0.12)",
        },
        loss: {
          DEFAULT: "#fb7185",
          light: "#fda4af",
          dark: "#e11d48",
          bg: "rgba(251, 113, 133, 0.12)",
        },
        accent: {
          DEFAULT: "#f5b942",
          light: "#fbd38d",
          dark: "#c9860f",
          bg: "rgba(245, 185, 66, 0.12)",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "Arial",
          "sans-serif",
        ],
      },
      boxShadow: {
        card: "0 1px 2px rgba(0,0,0,0.4), 0 1px 3px rgba(0,0,0,0.3)",
      },
    },
  },
  plugins: [],
};
