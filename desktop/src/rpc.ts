import { rpcUrl } from './apiConfig'

export type NotificationHandler = (params: any) => void

export class RpcError extends Error {
  constructor(public code: number, message: string, public data?: unknown) { super(message) }
}

class RpcClient {
  // 一个 WebSocket 同时承载请求响应和 Host 主动推送；pending 用请求 ID 关联结果。
  private socket: WebSocket | null = null
  private sequence = 0
  private pending = new Map<number, { resolve(value: any): void; reject(reason: any): void; timer:number }>()
  private handlers = new Map<string, Set<NotificationHandler>>()
  private reconnect?: number
  status: 'connecting' | 'connected' | 'offline' = 'offline'
  onStatus?: (status: RpcClient['status']) => void

  connect() {
    if (this.socket && this.socket.readyState <= WebSocket.OPEN) return
    this.status = 'connecting'; this.onStatus?.(this.status)
    this.socket = new WebSocket(rpcUrl())
    this.socket.onopen = () => { this.status = 'connected'; this.onStatus?.(this.status) }
    this.socket.onmessage = event => {
      let msg:any
      try { msg = JSON.parse(event.data) } catch { return }
      if (msg.id !== undefined && msg.id !== null) {
        const item = this.pending.get(msg.id); if (!item) return; this.pending.delete(msg.id); window.clearTimeout(item.timer)
        msg.error ? item.reject(new RpcError(msg.error.code, msg.error.message, msg.error.data)) : item.resolve(msg.result)
      } else if (msg.method) this.handlers.get(msg.method)?.forEach(handler => handler(msg.params))
    }
    this.socket.onclose = () => {
      this.status = 'offline'; this.onStatus?.(this.status)
      // 旧连接上的请求不会在重连后收到响应，因此立即拒绝并清理定时器。
      this.pending.forEach(item => {window.clearTimeout(item.timer);item.reject(new Error('Host 连接已断开，请确认后端仍在运行。'))}); this.pending.clear()
      if (this.reconnect) window.clearTimeout(this.reconnect)
      this.reconnect = window.setTimeout(() => this.connect(), 1500)
    }
  }
  async call<T = any>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    this.connect()
    // 启动阶段的调用复用正在连接的 socket，短暂等待 Host 就绪。
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      await new Promise<void>((resolve, reject) => {
        const started = Date.now(); const timer = window.setInterval(() => {
          if (this.socket?.readyState === WebSocket.OPEN) { clearInterval(timer); resolve() }
          else if (Date.now() - started > 6000) { clearInterval(timer); reject(new Error('无法连接 KoreAgent Next Host')) }
        }, 80)
      })
    }
    const id = ++this.sequence
    return new Promise<T>((resolve, reject) => {
      // Run 可能在等待工具审批，因此这两类请求采用更长的超时。
      const timeout = method==='conversation.send'||method==='run.recover'?30*60_000:30_000
      const timer=window.setTimeout(()=>{this.pending.delete(id);reject(new Error(`${method} 请求超时，请检查 Host 状态后重试。`))},timeout)
      this.pending.set(id, { resolve, reject, timer }); this.socket!.send(JSON.stringify({ jsonrpc: '2.0', id, method, params }))
    })
  }
  on(method: string, handler: NotificationHandler) {
    if (!this.handlers.has(method)) this.handlers.set(method, new Set())
    this.handlers.get(method)!.add(handler); return () => { this.handlers.get(method)?.delete(handler) }
  }
}

export const rpc = new RpcClient()

