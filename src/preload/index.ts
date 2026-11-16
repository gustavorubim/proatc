import { contextBridge, ipcRenderer } from 'electron'
import type { Connection, UpdateState } from '../shared/protocol'

// The only things the page can reach outside itself.
const api = {
  connection: (): Promise<Connection | null> => ipcRenderer.invoke('connection'),
  engineLog: (): Promise<{ lines: string[]; failure: string | null }> => ipcRenderer.invoke('engine-log'),
  onEngineLog: (listener: (line: string) => void): void => {
    ipcRenderer.on('engine-log', (_event, line: string) => listener(line))
  },
  onEngineExit: (listener: (message: string) => void): void => {
    ipcRenderer.on('engine-exit', (_event, message: string) => listener(message))
  },
  version: (): Promise<string> => ipcRenderer.invoke('app-version'),
  updateState: (): Promise<UpdateState> => ipcRenderer.invoke('update-state'),
  checkForUpdates: (): Promise<UpdateState> => ipcRenderer.invoke('update-check'),
  installUpdate: (): Promise<void> => ipcRenderer.invoke('update-install'),
  onUpdateState: (listener: (state: UpdateState) => void): void => {
    ipcRenderer.on('update-state', (_event, state: UpdateState) => listener(state))
  }
}

contextBridge.exposeInMainWorld('proatc', api)

export type ProAtcApi = typeof api
