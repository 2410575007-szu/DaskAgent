import { contextBridge, ipcRenderer } from 'electron'
// 只向渲染进程暴露经过限定的系统能力，避免直接启用 Node 集成。
contextBridge.exposeInMainWorld('koreNext', {
  openExternal: (url: string) => ipcRenderer.invoke('open-external', url),
  notify: (title: string, body: string) => ipcRenderer.invoke('notify', title, body),
  showApproval: () => ipcRenderer.invoke('show-approval'),
  hideApproval: () => ipcRenderer.invoke('hide-approval'),
})

