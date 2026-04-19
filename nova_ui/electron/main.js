// ═══════════════════════════════════════════════════════════════════════════
// NOVA Assistant — Electron main process.
//
// Responsibilities:
//   1. Spawn the Python FastAPI sidecar (nova_ui_server.py) as a subprocess
//      so the dashboard is self-contained — one click, everything runs.
//   2. Wait for the sidecar's /health to come online, then open a frameless
//      BrowserWindow pointed at http://127.0.0.1:7336/.
//   3. Add a system tray icon (always-on-top toggle, quit).
//   4. Clean up the Python child process on app quit.
//
// Launch: `npm install` once, then `npm start`, or run `..\start.bat` from
// the repo root.
// ═══════════════════════════════════════════════════════════════════════════
const { app, BrowserWindow, Tray, Menu, ipcMain, shell, nativeImage } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const http = require('http');
const fs   = require('fs');

const UI_PORT    = parseInt(process.env.NOVA_UI_PORT || '7336', 10);
const UI_HOST    = '127.0.0.1';
const UI_URL     = `http://${UI_HOST}:${UI_PORT}/`;
const HEALTH_URL = `http://${UI_HOST}:${UI_PORT}/health`;

// Project root = two levels up from electron/ (nova_ui/electron → nova)
const PROJECT_ROOT = path.resolve(__dirname, '..', '..');
const SERVER_PY    = path.join(PROJECT_ROOT, 'nova_ui_server.py');
const ASSISTANT_PY = path.join(PROJECT_ROOT, 'nova_assistant.py');
const GUARDIAN_PY  = path.join(PROJECT_ROOT, 'nova_tv_guardian.py');

let mainWindow    = null;
let tray          = null;
let pyProc        = null;  // nova_ui_server   (dashboard sidecar)
let assistProc    = null;  // nova_assistant   (voice + scheduler)
let guardianProc  = null;  // nova_tv_guardian (TradingView chart watchdog)
let voiceEnabled    = true;
let guardianEnabled = true;

// ── Python server boot ──────────────────────────────────────────────────────
function findPython() {
  // Prefer a local venv if present, else fall back to `python` on PATH.
  const candidates = [
    path.join(PROJECT_ROOT, '.venv', 'Scripts', 'python.exe'),
    path.join(PROJECT_ROOT, 'venv',  'Scripts', 'python.exe'),
    process.env.NOVA_PYTHON,
    'python',
  ].filter(Boolean);
  for (const c of candidates) {
    try { if (c === 'python' || fs.existsSync(c)) return c; } catch {}
  }
  return 'python';
}

function startPythonServer() {
  if (pyProc) return;
  const py = findPython();
  console.log(`[nova-desktop] launching ${py} ${SERVER_PY}`);
  pyProc = spawn(py, [SERVER_PY], {
    cwd:  PROJECT_ROOT,
    env:  { ...process.env, NOVA_UI_PORT: String(UI_PORT) },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  pyProc.stdout.on('data', (b) => process.stdout.write(`[py] ${b}`));
  pyProc.stderr.on('data', (b) => process.stderr.write(`[py] ${b}`));
  pyProc.on('exit', (code) => {
    console.warn(`[nova-desktop] python exited ${code}`);
    pyProc = null;
  });
}

function killPythonServer() {
  if (!pyProc) return;
  try { pyProc.kill(); } catch {}
  pyProc = null;
}

// ── Voice assistant (nova_assistant.py) ─────────────────────────────────────
function startAssistant() {
  if (assistProc) return;
  const py = findPython();
  console.log(`[nova-desktop] launching voice assistant ${py} ${ASSISTANT_PY}`);
  assistProc = spawn(py, [ASSISTANT_PY], {
    cwd:   PROJECT_ROOT,
    env:   { ...process.env, NOVA_UI_URL: UI_URL.replace(/\/$/, '') },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  assistProc.stdout.on('data', (b) => process.stdout.write(`[voice] ${b}`));
  assistProc.stderr.on('data', (b) => process.stderr.write(`[voice] ${b}`));
  assistProc.on('exit', (code) => {
    console.warn(`[nova-desktop] voice assistant exited ${code}`);
    assistProc = null;
  });
}

function killAssistant() {
  if (!assistProc) return;
  try { assistProc.kill(); } catch {}
  assistProc = null;
}

function setVoice(on) {
  voiceEnabled = !!on;
  if (voiceEnabled) startAssistant();
  else killAssistant();
}

// ── TradingView Guardian (nova_tv_guardian.py) ──────────────────────────────
function startGuardian() {
  if (guardianProc) return;
  const py = findPython();
  console.log(`[nova-desktop] launching TV guardian ${py} ${GUARDIAN_PY}`);
  guardianProc = spawn(py, [GUARDIAN_PY], {
    cwd:   PROJECT_ROOT,
    env:   { ...process.env, NOVA_UI_URL: UI_URL.replace(/\/$/, '') },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  guardianProc.stdout.on('data', (b) => process.stdout.write(`[guardian] ${b}`));
  guardianProc.stderr.on('data', (b) => process.stderr.write(`[guardian] ${b}`));
  guardianProc.on('exit', (code) => {
    console.warn(`[nova-desktop] TV guardian exited ${code}`);
    guardianProc = null;
  });
}

function killGuardian() {
  if (!guardianProc) return;
  try { guardianProc.kill(); } catch {}
  guardianProc = null;
}

function setGuardian(on) {
  guardianEnabled = !!on;
  if (guardianEnabled) startGuardian();
  else killGuardian();
}

function waitForHealth(maxMs = 30000, intervalMs = 400) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + maxMs;
    const tick = () => {
      const req = http.get(HEALTH_URL, (res) => {
        if (res.statusCode === 200) return resolve();
        res.resume();
        schedule();
      });
      req.on('error', schedule);
      req.setTimeout(1500, () => { req.destroy(); schedule(); });
    };
    const schedule = () => {
      if (Date.now() > deadline) return reject(new Error('server never came up'));
      setTimeout(tick, intervalMs);
    };
    tick();
  });
}

// ── Window ──────────────────────────────────────────────────────────────────
function createWindow() {
  mainWindow = new BrowserWindow({
    width:     1280,
    height:    820,
    minWidth:  1100,
    minHeight: 700,
    frame:     false,
    transparent: false,
    backgroundColor: '#06101d',
    title:     'NOVA Assistant',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration:  false,
      backgroundThrottling: false,
    },
  });

  mainWindow.loadURL(UI_URL);
  mainWindow.on('closed', () => { mainWindow = null; });

  // External links open in the default browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
}

// ── Tray ────────────────────────────────────────────────────────────────────
function createTray() {
  // Simple 16x16 cyan dot for the tray — no icon file needed.
  const img = nativeImage.createFromDataURL(
    'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAMAAAAoLQ9TAAAAQlBMVEUAAAAA5f8A5f8A5f8A5f8A5f8A5f8A5f8A5f8A5f8A5f8A5f8A5f8A5f8A5f8A5f8A5f8A5f8A5f8A5f8A5f8AAAB9n7ryAAAAFXRSTlMAECAwQFBgcICQoLDA0ODw////AP//ZSvZUAAAAHZJREFUeNpjYAADRiZmFlY2dg5OLm4eXgY+BkEhYRFRMXEJSSlpGVk5eQUGBkUlZRVVNXUNTS1tHV09fQMGIyMTUzNzC0srGxs7ewdHJwZnF1c3dw9PLx9fP/+AQKGgkNCwiMio6JjYuPjEpOSU1LT0jMysbABTzw6ERgApswAAAABJRU5ErkJggg==',
  );
  tray = new Tray(img);
  tray.setToolTip('NOVA Assistant');
  const contextMenu = Menu.buildFromTemplate([
    { label: 'Show NOVA',
      click: () => { if (mainWindow) mainWindow.show(); else createWindow(); } },
    { label: 'Hide',
      click: () => mainWindow && mainWindow.hide() },
    { type: 'separator' },
    { label: 'Always on top',
      type:  'checkbox',
      checked: false,
      click: (item) => mainWindow && mainWindow.setAlwaysOnTop(item.checked) },
    { label: 'Voice (alerts + briefings)',
      type:  'checkbox',
      checked: voiceEnabled,
      click: (item) => setVoice(item.checked) },
    { label: 'TV Guardian (chart watchdog)',
      type:  'checkbox',
      checked: guardianEnabled,
      click: (item) => setGuardian(item.checked) },
    { label: 'Reload',
      click: () => mainWindow && mainWindow.reload() },
    { type: 'separator' },
    { label: 'Quit NOVA',
      click: () => { app.isQuitting = true; app.quit(); } },
  ]);
  tray.setContextMenu(contextMenu);
  tray.on('click', () => {
    if (!mainWindow) createWindow();
    else if (mainWindow.isVisible()) mainWindow.hide();
    else mainWindow.show();
  });
}

// ── Custom titlebar IPC (window controls) ───────────────────────────────────
ipcMain.handle('win-minimize', () => mainWindow && mainWindow.minimize());
ipcMain.handle('win-maximize', () => {
  if (!mainWindow) return;
  if (mainWindow.isMaximized()) mainWindow.unmaximize();
  else mainWindow.maximize();
});
ipcMain.handle('win-close', () => mainWindow && mainWindow.hide());
ipcMain.handle('win-quit',  () => { app.isQuitting = true; app.quit(); });

// ── App lifecycle ───────────────────────────────────────────────────────────
app.whenReady().then(async () => {
  startPythonServer();
  try {
    await waitForHealth();
    console.log('[nova-desktop] sidecar online');
  } catch (e) {
    console.warn('[nova-desktop]', e.message);
  }
  // Boot the voice assistant AFTER the sidecar is healthy so its initial
  // push_log/push_mode calls land on a running server.
  if (voiceEnabled)    startAssistant();
  if (guardianEnabled) startGuardian();
  createWindow();
  createTray();
});

app.on('window-all-closed', (e) => {
  // Keep alive in tray instead of quitting — 24/7 behavior.
  if (!app.isQuitting) e.preventDefault?.();
});
app.on('before-quit', () => { app.isQuitting = true; });
app.on('will-quit',   () => { killGuardian(); killAssistant(); killPythonServer(); });
process.on('exit',    () => { killGuardian(); killAssistant(); killPythonServer(); });
