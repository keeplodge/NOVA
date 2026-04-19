// ═══════════════════════════════════════════════════════════════════════════
// NOVA Assistant Dashboard — Three.js wireframe orb + live-state renderer.
// Mirrors the Neural Brain's core orb aesthetic. Connects to the Python
// sidecar server at :7336 via WebSocket for real-time state updates.
// ═══════════════════════════════════════════════════════════════════════════
import * as THREE from 'three';
import { EffectComposer }  from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass }      from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';

// ── Colour tokens (match styles.css) ─────────────────────────────────────────
const COL = {
  cyan:    new THREE.Color(0x00e5ff),
  white:   new THREE.Color(0xf0ffff),
  orange:  new THREE.Color(0xff6b00),
  red:     new THREE.Color(0xe53e3e),
  green:   new THREE.Color(0x00c853),
  amber:   new THREE.Color(0xff9d00),
  purple:  new THREE.Color(0xb072ff),
};

// ═══════════════════════════════════════════════════════════════════════════
// WireframeOrb — Fibonacci-distributed nodes + nearest-neighbor edges + chord
// lines through the interior + bright inner core. Echoes the Brain's BrainCore.
// ═══════════════════════════════════════════════════════════════════════════
class WireframeOrb {
  constructor(scene) {
    this.scene = scene;
    this.time = 0;
    this.color = COL.cyan.clone();
    this.targetColor = COL.cyan.clone();
    this.mode = 'idle';

    const RADIUS    = 0.26;
    const NODES     = 110;
    const NEIGHBORS = 4;

    // Fibonacci sphere distribution
    const nodes = [];
    const golden = Math.PI * (3 - Math.sqrt(5));
    for (let i = 0; i < NODES; i++) {
      const y = 1 - (i / (NODES - 1)) * 2;
      const r = Math.sqrt(1 - y * y);
      const th = golden * i;
      nodes.push(new THREE.Vector3(
        Math.cos(th) * r * RADIUS,
        y * RADIUS,
        Math.sin(th) * r * RADIUS,
      ));
    }

    // Edges — each node connects to its NEIGHBORS nearest (deduped)
    const edges = [];
    const seen = new Set();
    for (let i = 0; i < nodes.length; i++) {
      const d = nodes.map((n, j) => ({ j, d: nodes[i].distanceToSquared(n) }));
      d.sort((a, b) => a.d - b.d);
      for (let k = 1; k <= NEIGHBORS; k++) {
        const j = d[k].j;
        const key = i < j ? `${i}-${j}` : `${j}-${i}`;
        if (seen.has(key)) continue;
        seen.add(key);
        edges.push([i, j]);
      }
    }

    const epos = new Float32Array(edges.length * 6);
    const ecol = new Float32Array(edges.length * 6);
    for (let e = 0; e < edges.length; e++) {
      const [a, b] = edges[e];
      const na = nodes[a], nb = nodes[b];
      epos[e*6]   = na.x; epos[e*6+1] = na.y; epos[e*6+2] = na.z;
      epos[e*6+3] = nb.x; epos[e*6+4] = nb.y; epos[e*6+5] = nb.z;
      const bright = Math.random() < 0.12;
      const tint = bright ? COL.white : COL.cyan;
      ecol[e*6]   = tint.r; ecol[e*6+1] = tint.g; ecol[e*6+2] = tint.b;
      ecol[e*6+3] = tint.r; ecol[e*6+4] = tint.g; ecol[e*6+5] = tint.b;
    }
    const edgeGeo = new THREE.BufferGeometry();
    edgeGeo.setAttribute('position', new THREE.BufferAttribute(epos, 3));
    edgeGeo.setAttribute('color',    new THREE.BufferAttribute(ecol, 3));
    this.edges = new THREE.LineSegments(edgeGeo, new THREE.LineBasicMaterial({
      vertexColors: true, transparent: true, opacity: 0.8,
      blending: THREE.AdditiveBlending, depthWrite: false,
    }));
    scene.add(this.edges);

    // Interior chord "data lightning"
    const CHORDS = 80;
    const cpos = new Float32Array(CHORDS * 6);
    for (let i = 0; i < CHORDS; i++) {
      const a = nodes[Math.floor(Math.random() * nodes.length)];
      const b = nodes[Math.floor(Math.random() * nodes.length)];
      cpos[i*6]   = a.x; cpos[i*6+1] = a.y; cpos[i*6+2] = a.z;
      cpos[i*6+3] = b.x; cpos[i*6+4] = b.y; cpos[i*6+5] = b.z;
    }
    const chordGeo = new THREE.BufferGeometry();
    chordGeo.setAttribute('position', new THREE.BufferAttribute(cpos, 3));
    this.chords = new THREE.LineSegments(chordGeo, new THREE.LineBasicMaterial({
      color: 0x33b8e8, transparent: true, opacity: 0.22,
      blending: THREE.AdditiveBlending, depthWrite: false,
    }));
    scene.add(this.chords);

    // Node sprites
    const npos = new Float32Array(NODES * 3);
    const ncol = new Float32Array(NODES * 3);
    for (let i = 0; i < NODES; i++) {
      npos[i*3]   = nodes[i].x;
      npos[i*3+1] = nodes[i].y;
      npos[i*3+2] = nodes[i].z;
      const bright = Math.random() < 0.22;
      const tint = bright ? COL.white : COL.cyan;
      ncol[i*3] = tint.r; ncol[i*3+1] = tint.g; ncol[i*3+2] = tint.b;
    }
    const nodeGeo = new THREE.BufferGeometry();
    nodeGeo.setAttribute('position', new THREE.BufferAttribute(npos, 3));
    nodeGeo.setAttribute('color',    new THREE.BufferAttribute(ncol, 3));

    // Soft glow sprite for nodes
    const tc = document.createElement('canvas');
    tc.width = tc.height = 64;
    const tctx = tc.getContext('2d');
    const g = tctx.createRadialGradient(32,32,0,32,32,32);
    g.addColorStop(0,   'rgba(255,255,255,1)');
    g.addColorStop(0.2, 'rgba(255,255,255,0.8)');
    g.addColorStop(0.5, 'rgba(0,229,255,0.4)');
    g.addColorStop(1,   'rgba(0,229,255,0)');
    tctx.fillStyle = g;
    tctx.fillRect(0, 0, 64, 64);

    this.nodes = new THREE.Points(nodeGeo, new THREE.PointsMaterial({
      size: 0.05, map: new THREE.CanvasTexture(tc), vertexColors: true,
      transparent: true, opacity: 1.0,
      blending: THREE.AdditiveBlending, depthWrite: false, sizeAttenuation: true,
    }));
    scene.add(this.nodes);

    // Inner bright core
    const coreGeo = new THREE.SphereGeometry(0.05, 24, 24);
    const coreMat = new THREE.MeshBasicMaterial({
      color: 0xffffff, transparent: true, opacity: 0.95,
      blending: THREE.AdditiveBlending, depthWrite: false,
    });
    this.core = new THREE.Mesh(coreGeo, coreMat);
    scene.add(this.core);

    // Soft halo glow behind everything
    const haloGeo = new THREE.SphereGeometry(RADIUS * 1.8, 48, 48);
    this.halo = new THREE.Mesh(haloGeo, new THREE.MeshBasicMaterial({
      color: 0x00c8ff, transparent: true, opacity: 0.05,
      blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.BackSide,
    }));
    scene.add(this.halo);
  }

  setMode(mode) {
    this.mode = mode;
    switch (mode) {
      case 'listening': this.targetColor = COL.cyan.clone();   break;
      case 'speaking':  this.targetColor = COL.white.clone();  break;
      case 'alert':     this.targetColor = COL.red.clone();    break;
      case 'trade':     this.targetColor = COL.green.clone();  break;
      default:          this.targetColor = COL.cyan.clone();
    }
  }

  setTintColor(hex) {
    this.targetColor = new THREE.Color(hex);
  }

  update(dt) {
    this.time += dt;
    this.color.lerp(this.targetColor, 0.08);

    // Edge opacity flicker
    const flicker = 0.75 + Math.sin(this.time * 4.2) * 0.08 + Math.random() * 0.04;
    this.edges.material.opacity = flicker;

    // Chord opacity slow pulse
    this.chords.material.opacity = 0.18 + 0.08 * Math.sin(this.time * 2.1);

    // Node size + opacity pulse
    this.nodes.material.opacity = 0.9 + Math.sin(this.time * 1.8) * 0.08;
    const activityBoost = (this.mode === 'speaking' || this.mode === 'alert' || this.mode === 'trade') ? 0.02 : 0;
    this.nodes.material.size = 0.045 + activityBoost + Math.sin(this.time * 1.2) * 0.004;

    // Core pulse + color
    const pulse = 0.85 + Math.sin(this.time * 2.2) * 0.15;
    this.core.material.opacity = pulse * 0.95;
    this.core.scale.setScalar(1.0 + Math.sin(this.time * 2.4) * 0.12);
    this.core.material.color.copy(this.color);

    // Halo
    this.halo.material.opacity = 0.05 + 0.05 * Math.sin(this.time * 0.8);
    this.halo.scale.setScalar(1.0 + Math.sin(this.time * 0.6) * 0.08);

    // Rotation
    const rotY = dt * 0.22;
    const rotX = dt * 0.06;
    const rotZ = dt * 0.03;
    this.edges.rotation.y  += rotY;  this.edges.rotation.x  += rotX;  this.edges.rotation.z  += rotZ;
    this.chords.rotation.y += rotY;  this.chords.rotation.x += rotX;  this.chords.rotation.z += rotZ;
    this.nodes.rotation.y  += rotY;  this.nodes.rotation.x  += rotX;  this.nodes.rotation.z  += rotZ;
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Waveform — 2D canvas bars synced with the orb mode.
// ═══════════════════════════════════════════════════════════════════════════
class Waveform {
  constructor(canvas) {
    this.c = canvas;
    this.ctx = canvas.getContext('2d');
    this.N = 48;
    this.heights = new Array(this.N).fill(4);
    this.targets = new Array(this.N).fill(4);
    this.phase = 0;
    this.mode = 'idle';
    this.color = '#00e5ff';
  }
  setMode(mode)  { this.mode = mode; }
  setColor(hex)  { this.color = hex; }
  draw(dt) {
    const ctx = this.ctx;
    const w = this.c.width  = this.c.clientWidth  * (window.devicePixelRatio || 1);
    const h = this.c.height = this.c.clientHeight * (window.devicePixelRatio || 1);
    ctx.clearRect(0, 0, w, h);
    this.phase += dt * 2;
    const H = h, W = w;
    const barSlot = W / this.N;
    const barW    = Math.max(1, barSlot * 0.55);
    const barMax  = H * 0.72;

    for (let i = 0; i < this.N; i++) {
      if (this.mode === 'speaking') {
        if (Math.random() < 0.35) this.targets[i] = 6 + Math.random() * barMax;
      } else if (this.mode === 'alert' || this.mode === 'trade') {
        this.targets[i] = barMax * 0.82 * Math.abs(Math.sin(this.phase * 2.3 + i * Math.PI / this.N));
      } else if (this.mode === 'listening') {
        this.targets[i] = barMax * 0.35 * Math.abs(Math.sin(this.phase * 0.9 + i * (Math.PI * 2 / this.N))) + 4;
      } else {
        this.targets[i] = barMax * 0.08 * Math.abs(Math.sin(this.phase * 0.3 + i * (Math.PI / this.N))) + 3;
      }
      this.heights[i] += (this.targets[i] - this.heights[i]) * 0.25;

      const hgt = Math.max(2, this.heights[i]);
      const x0 = i * barSlot + (barSlot - barW) / 2;
      const y0 = (H - hgt) / 2;
      ctx.fillStyle = this.color;
      ctx.shadowColor = this.color;
      ctx.shadowBlur  = 8;
      ctx.fillRect(x0, y0, barW, hgt);
    }
    ctx.shadowBlur = 0;
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// App — wires scene + state + WebSocket
// ═══════════════════════════════════════════════════════════════════════════
class App {
  constructor() {
    this.state = {
      mode: 'idle',
      color: '#00e5ff',
      session: '—',
      nq: '—',
      vix: '—',
      daily_loss: '—',
      remaining: '—',
      trades_today: '—',
      last_signal: '—',
      brain_status: 'CHECKING',
      brain_memories: '—',
      next_session: '—',
      next_session_sub: '',
      status_text: 'INITIALIZING',
      neuro: { trading: 0.4, ideas: 0.3, nova: 0.5, personal: 0.2 },
    };
    this.log = [];
    this.queryCount = 0;
  }

  async init() {
    this._setLoading(10, 'CREATING 3D SCENE...');
    this._initScene();
    this._setLoading(40, 'BUILDING WIREFRAME ORB...');
    this.orb = new WireframeOrb(this.scene);
    this._setLoading(60, 'WIRING WAVEFORM...');
    this.wave = new Waveform(document.getElementById('wave-canvas'));
    this._setLoading(75, 'CONNECTING TO ASSISTANT...');
    await this._connectWS();
    this._setLoading(95, 'RENDERING...');
    this._animate();
    setTimeout(() => {
      this._setLoading(100, 'ASSISTANT ONLINE');
      setTimeout(() => document.getElementById('loading').classList.add('hidden'), 400);
    }, 300);
  }

  _setLoading(pct, msg) {
    const f = document.getElementById('loading-fill');
    const s = document.getElementById('loading-status');
    if (f) f.style.width = `${pct}%`;
    if (s) s.textContent = msg;
  }

  _initScene() {
    const container = document.getElementById('canvas-container');
    this.scene = new THREE.Scene();
    this.scene.background = null;

    this.camera = new THREE.PerspectiveCamera(45, window.innerWidth / window.innerHeight, 0.01, 100);
    this.camera.position.set(0, 0, 1.6);

    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
    this.renderer.setSize(window.innerWidth, window.innerHeight);
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.1;
    container.appendChild(this.renderer.domElement);

    this.composer = new EffectComposer(this.renderer);
    this.composer.addPass(new RenderPass(this.scene, this.camera));
    this.bloomPass = new UnrealBloomPass(new THREE.Vector2(window.innerWidth, window.innerHeight), 0.7, 0.42, 0.35);
    this.composer.addPass(this.bloomPass);

    window.addEventListener('resize', () => {
      this.camera.aspect = window.innerWidth / window.innerHeight;
      this.camera.updateProjectionMatrix();
      this.renderer.setSize(window.innerWidth, window.innerHeight);
      this.composer.setSize(window.innerWidth, window.innerHeight);
    });
  }

  async _connectWS() {
    const url = `ws://${location.host}/ws`;
    return new Promise((resolve) => {
      const connect = () => {
        this.ws = new WebSocket(url);
        this.ws.onopen = () => {
          console.log('[nova-ui] WebSocket connected');
          resolve();
        };
        this.ws.onmessage = (e) => this._handleMessage(e.data);
        this.ws.onerror = () => {};
        this.ws.onclose = () => {
          console.warn('[nova-ui] WebSocket closed, retrying in 2s');
          setTimeout(connect, 2000);
        };
      };
      connect();
      // Resolve after 1s even if WS hasn't opened yet so the UI shows.
      setTimeout(resolve, 1000);
    });
  }

  _handleMessage(raw) {
    let msg;
    try { msg = JSON.parse(raw); } catch { return; }

    if (msg.type === 'snapshot' || msg.type === 'state') {
      Object.assign(this.state, msg.payload || {});
      this._applyState();
    } else if (msg.type === 'mode') {
      this.state.mode = msg.payload.mode || 'idle';
      if (msg.payload.color) this.state.color = msg.payload.color;
      this._applyState();
    } else if (msg.type === 'log') {
      this._pushLog(msg.payload);
    }
  }

  _applyState() {
    const s = this.state;

    // Mode + color propagate to orb + waveform
    this.orb.setMode(s.mode);
    this.orb.setTintColor(s.color);
    this.wave.setMode(s.mode);
    this.wave.setColor(s.color);

    // Stat panels
    _text('stat-session',   s.session);
    _text('stat-nq',        s.nq);
    _text('stat-vix',       s.vix);
    _text('stat-brain-status', s.brain_status);
    _text('stat-memories',  s.brain_memories);
    _text('stat-loss',      s.daily_loss);
    _text('stat-remaining', s.remaining);
    _text('stat-trades',    s.trades_today);
    _text('stat-last-signal', s.last_signal);
    _text('stat-next-session', s.next_session);
    _text('stat-next-sub',     s.next_session_sub);

    // Mode chips
    _modeChip('mode-badge',   s.mode);
    _modeChip('market-badge', s.mode);
    _modeChip('today-badge',  s.mode);

    // Footer
    _text('footer-status', s.status_text || s.mode.toUpperCase());
    const brainOnline = (s.brain_status || '').toUpperCase() === 'ONLINE';
    const dot1 = document.getElementById('link-dot');
    const dot2 = document.getElementById('footer-dot');
    [dot1, dot2].forEach((d) => {
      if (!d) return;
      d.style.background = brainOnline ? '#00C853' : '#E53E3E';
      d.style.boxShadow  = `0 0 8px ${brainOnline ? 'rgba(0,200,83,0.8)' : 'rgba(229,62,62,0.8)'}`;
    });

    // Neuromodulator bars
    if (s.neuro) {
      _barFill('bar-trading',  s.neuro.trading);
      _barFill('bar-ideas',    s.neuro.ideas);
      _barFill('bar-nova',     s.neuro.nova);
      _barFill('bar-personal', s.neuro.personal);
    }
  }

  _pushLog(entry) {
    if (!entry || !entry.msg) return;
    this.log.unshift({
      time: entry.time || new Date().toISOString().slice(11, 19),
      kind: entry.kind || 'state',
      msg:  entry.msg,
    });
    if (this.log.length > 200) this.log.length = 200;
    const list = document.getElementById('log-list');
    list.innerHTML = this.log.slice(0, 60).map((l) => `
      <div class="log-row">
        <span class="log-time">${_esc(l.time)}</span>
        <span class="log-kind ${_esc(l.kind)}">${_esc(l.kind)}</span>
        <span class="log-msg">${_esc(l.msg)}</span>
      </div>
    `).join('');
  }

  _animate() {
    let last = performance.now();
    const loop = () => {
      requestAnimationFrame(loop);
      const now = performance.now();
      const dt  = Math.min((now - last) / 1000, 0.1);
      last = now;
      if (this.orb)  this.orb.update(dt);
      if (this.wave) this.wave.draw(dt);
      this.composer.render();
    };
    loop();
  }
}

// ─── DOM helpers ─────────────────────────────────────────────────────────────
function _text(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = (val === null || val === undefined || val === '') ? '—' : String(val);
}
function _modeChip(id, mode) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = (mode || 'idle').toUpperCase();
  el.className = 'state-chip mode-' + (mode || 'idle');
}
function _barFill(id, pct) {
  const el = document.getElementById(id);
  if (!el) return;
  const clamped = Math.max(0, Math.min(1, Number(pct) || 0));
  el.style.width = `${clamped * 100}%`;
}
function _esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;',
  }[c]));
}

// ─── Boot ────────────────────────────────────────────────────────────────────
new App().init().catch(console.error);
