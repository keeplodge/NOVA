// Preload bridge — exposes a tiny window-control API to the renderer so the
// custom titlebar can minimize / maximize / close / quit without nodeIntegration.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('novaDesktop', {
  minimize: () => ipcRenderer.invoke('win-minimize'),
  maximize: () => ipcRenderer.invoke('win-maximize'),
  close:    () => ipcRenderer.invoke('win-close'),
  quit:     () => ipcRenderer.invoke('win-quit'),
  isDesktop: true,
});
