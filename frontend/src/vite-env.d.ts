/// <reference types="vite/client" />
interface Window {
  ethereum?: { request(args: { method: string; params?: unknown[] }): Promise<unknown> };
  __HYPERCOPY_CONFIG__?: { API_BASE_URL?: string; WS_URL?: string };
}
