/// <reference types="vite/client" />
import type { ProAtcApi } from '../../preload'

declare global {
  interface Window {
    proatc?: ProAtcApi
  }
}
