import type { Config } from "tailwindcss";

// ═══ 主题化：所有语义颜色改为 CSS 变量 (RGB 通道), 由 globals.css 的
//    :root (暗) 与 [data-theme="light"] (亮) 两套值驱动。
//    关键技巧：把内置 `white` 也重定义为跟随主题的变量 ——
//    这样全站散落的 text-white / bg-white/xx / border-white/xx 在亮色模式
//    会自动变成"近黑/深色微染", 无需逐个组件改 class。
//    透明度修饰符 (/60, /[0.06]) 依赖 `rgb(var(--x) / <alpha-value>)` 写法。
const v = (name: string) => `rgb(var(${name}) / <alpha-value>)`;

export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // 内置 white 重定义为跟随主题 (暗=近白 / 亮=近黑)
        white: v("--color-fg"),
        accent: {
          DEFAULT: v("--color-accent"),
          dark: v("--color-accent-dark"),
          card: v("--color-accent-card"),
          inner: v("--color-accent-inner"),
          dim: v("--color-accent-dim"),
        },
        field: {
          50: "#edfdf5", 100: "#d3fae5", 200: "#aaf5ce", 300: "#73eab0",
          400: v("--color-field-400"),
          500: v("--color-field-500"),
          600: v("--color-field-600"),
          700: "#0f7a4b", 800: "#10603d", 900: "#0e4f34", 950: "#052e1c",
        },
        pitch: {
          50: "#edfdf5", 100: "#d3fae5", 200: "#aaf5ce",
          300: v("--color-pitch-300"),
          400: v("--color-pitch-400"),
          500: v("--color-pitch-500"),
          600: v("--color-pitch-600"),
          700: "#0f7a4b", 800: "#10603d", 900: "#0e4f34", 950: "#052e1c",
        },
        ember: {
          50: "#fffbf0", 100: "#fef4d9", 200: "#fde4a8", 300: v("--color-ember-300"),
          400: v("--color-ember-400"),
          500: v("--color-ember-500"),
          600: "#d97706", 700: "#b45309", 800: "#92400e", 900: "#78350f", 950: "#451a03",
        },
        danger: {
          50: "#fef5f5", 100: "#fde8e8", 200: "#fbd0d0", 300: "#f7aaaa",
          400: v("--color-danger-400"),
          500: v("--color-danger-500"),
          600: v("--color-danger-600"),
          700: "#b81c20", 800: "#991a1e", 900: "#7f1d20", 950: "#450a0c",
        },
        frost: {
          50: "#f0f7ff", 100: "#e0effe", 200: "#baddfd",
          300: v("--color-frost-300"),
          400: v("--color-frost-400"),
          500: v("--color-frost-500"),
          600: "#0868c7", 700: "#0952a1", 800: "#0d4785", 900: "#123c6e", 950: "#0c2649",
        },
        surface: {
          canvas: v("--color-surface-canvas"),
          dark: v("--color-surface-dark"),
          panel: v("--color-surface-panel"),
          card: v("--color-surface-card"),
          hover: v("--color-surface-hover"),
          border: v("--color-surface-border"),
        },
        ink: {
          primary: v("--color-ink-primary"),
          secondary: v("--color-ink-secondary"),
          muted: v("--color-ink-muted"),
          disabled: v("--color-ink-disabled"),
          inverse: "#0A0A0B",
          "inverse-secondary": "#141417",
        },
      },
      fontFamily: {
        // 系统字体优先 — Microsoft YaHei/Segoe UI (Windows) / PingFang/SF Pro (Mac)
        // Inter 仅作尾备, 本地若安装则启用, 否则降级到 sans-serif
        display: ['Inter', 'system-ui', '-apple-system', '"Segoe UI"', '"Microsoft YaHei"', '"PingFang SC"', 'sans-serif'],
        body: ['Inter', 'system-ui', '-apple-system', '"Segoe UI"', '"Microsoft YaHei"', '"PingFang SC"', 'sans-serif'],
        mono: ['ui-monospace', '"Cascadia Code"', '"JetBrains Mono"', 'Consolas', 'monospace'],
      },
      borderRadius: {
        sm: "4px", md: "6px", lg: "8px", xl: "12px", "2xl": "16px", full: "9999px",
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
