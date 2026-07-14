import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        field: {
          50: "#edfdf5", 100: "#d3fae5", 200: "#aaf5ce", 300: "#73eab0",
          400: "#3ecf8e", 500: "#1fb873", 600: "#13985c", 700: "#0f7a4b",
          800: "#10603d", 900: "#0e4f34", 950: "#052e1c",
        },
        pitch: {
          50: "#edfdf5", 100: "#d3fae5", 200: "#aaf5ce", 300: "#73eab0",
          400: "#3ecf8e", 500: "#1fb873", 600: "#13985c", 700: "#0f7a4b",
          800: "#10603d", 900: "#0e4f34", 950: "#052e1c",
        },
        ember: {
          50: "#fffbf0", 100: "#fef4d9", 200: "#fde4a8", 300: "#fccf6d",
          400: "#fab83e", 500: "#f59e0b", 600: "#d97706", 700: "#b45309",
          800: "#92400e", 900: "#78350f", 950: "#451a03",
        },
        danger: {
          50: "#fef5f5", 100: "#fde8e8", 200: "#fbd0d0", 300: "#f7aaaa",
          400: "#f07a7a", 500: "#e5484d", 600: "#da2e34", 700: "#b81c20",
          800: "#991a1e", 900: "#7f1d20", 950: "#450a0c",
        },
        frost: {
          50: "#f0f7ff", 100: "#e0effe", 200: "#baddfd", 300: "#7ec2fc",
          400: "#3ea2f8", 500: "#1485e9", 600: "#0868c7", 700: "#0952a1",
          800: "#0d4785", 900: "#123c6e", 950: "#0c2649",
        },
        surface: {
          canvas: "#0d1f17",
          dark: "#091410",
          panel: "#15261f",
          card: "#1a2d25",
          hover: "#1f352c",
          border: "rgba(62,207,142,0.08)",
          light: "#fafafa",
          "light-card": "#ffffff",
          "light-border": "#e8e8e8",
        },
        ink: {
          primary: "rgba(255,255,255,0.92)",
          secondary: "rgba(255,255,255,0.64)",
          muted: "rgba(255,255,255,0.40)",
          disabled: "rgba(255,255,255,0.22)",
          inverse: "#171717",
          "inverse-secondary": "#4a4a4a",
        },
      },
      fontFamily: {
        display: ["Inter", "sans-serif"],
        body: ["Inter", "sans-serif"],
        mono: ['"JetBrains Mono"', "monospace"],
      },
      borderRadius: {
        sm: "4px",
        md: "6px",
        lg: "8px",
        xl: "12px",
        "2xl": "16px",
        full: "9999px",
      },
      fontSize: {
        micro: ["10px", { lineHeight: "1.4", letterSpacing: "0.02em" }],
        stat: ["28px", { lineHeight: "1.2", letterSpacing: "-0.02em" }],
      },
      animation: {
        "fade-in": "fadeIn 0.3s ease-out",
        "slide-up": "slideUp 0.3s ease-out",
      },
      keyframes: {
        fadeIn: { "0%": { opacity: "0" }, "100%": { opacity: "1" } },
        slideUp: {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
      },
    },
  },
  plugins: [],
} satisfies Config;
