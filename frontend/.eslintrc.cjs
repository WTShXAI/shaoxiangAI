/* eslint-disable no-undef */
// 哨响AI 前端 ESLint 配置 (2026-08-31 Fix-5d)
// 原则: 编译期(tsc noUnusedLocals/Parameters)已兜底未用变量, 此处聚焦
//   - 未用变量/参数 (与 tsc 双保险, 并捕获 import 了但只当类型用的场景)
//   - 引号/分号/尾逗号一致性
//   - 禁 any / 禁空函数 / 禁 console 残留
// 离线可跑: eslint 8 + @typescript-eslint v7, 无网络依赖.
module.exports = {
  root: true,
  env: { browser: true, es2020: true },
  parser: '@typescript-eslint/parser',
  parserOptions: {
    ecmaVersion: 2020,
    sourceType: 'module',
    ecmaFeatures: { jsx: true },
  },
  plugins: ['@typescript-eslint', 'react-hooks'],
  settings: {
    react: { version: '18.3' },
  },
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
  ],
  ignorePatterns: ['dist/', 'node_modules/', 'test-results/', '*.config.ts'],
  rules: {
    // ── 未用代码 (双保险, tsc 已硬查) ──
    '@typescript-eslint/no-unused-vars': [
      'warn',
      { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrors: 'none' },
    ],
    // ── 风格一致性 ──
    'quotes': ['warn', 'single', { avoidEscape: true }],
    'semi': ['warn', 'never'],
    'comma-dangle': ['warn', 'always-multiline'],
    // ── 正确性 ──
    '@typescript-eslint/no-explicit-any': 'warn',
    'no-console': ['warn', { allow: ['warn', 'error'] }],
    'no-empty': ['warn', { allowEmptyCatch: true }],
    // ── React 特定 (react/react-in-jsx-scope 不需要: jsx: react-jsx 运行时自动注入) ──
  },
}
