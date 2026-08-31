/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** bridge_service 地址 (默认同源空串; Docker/远程部署时填 http://<host>:9000) */
  readonly VITE_BRIDGE_URL?: string
  /** 生产鉴权 Key (默认不注入) */
  readonly VITE_API_KEY?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
