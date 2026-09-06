import React from 'react'

interface Props {
  children: React.ReactNode
  fallback?: React.ReactNode
}
interface State {
  hasError: boolean
  message: string
}

/**
 * 全局错误边界 (E4 P1-13).
 * 捕获渲染期异常, 防止单页崩溃导致整站白屏; 显示可恢复提示而非黑屏.
 */
export default class ErrorBoundary extends React.Component<Props, State> {
  constructor(props: Props) {
    super(props)
    this.state = { hasError: false, message: '' }
  }

  static getDerivedStateFromError(error: unknown): State {
    return {
      hasError: true,
      message: error instanceof Error ? error.message : String(error),
    }
  }

  componentDidCatch(error: unknown, info: React.ErrorInfo) {
    // 生产环境可接 Sentry; 当前仅 console 记录
    console.error('[ErrorBoundary] 渲染异常:', error, info.componentStack)
  }

  handleReset = () => this.setState({ hasError: false, message: '' })

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback
      return (
        <div className="min-h-screen flex items-center justify-center bg-[#0a0e0f] text-ink">
          <div className="max-w-md w-full mx-4 p-6 rounded-2xl border border-danger-600/30 bg-white/[0.03] backdrop-blur-xl">
            <h2 className="text-lg font-bold text-danger-400 mb-2">页面渲染出错</h2>
            <p className="text-sm text-ink-secondary mb-4 break-words">{this.state.message}</p>
            <button
              onClick={this.handleReset}
              className="px-4 py-2 rounded-lg bg-pitch-600 hover:bg-pitch-500 text-white text-sm font-semibold transition-colors"
            >
              重试
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
