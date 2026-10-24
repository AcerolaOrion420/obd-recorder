// FleetPulse OBD-II Recorder Web UI Controller
// Offline-ready, Zero External Dependencies

(function() {
  'use strict';

  // --- Audio Synthesis via WebAudio API ---
  let audioCtx = null;

  function initAudio() {
    if (!audioCtx) {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (AudioContext) {
        audioCtx = new AudioContext();
      }
    }
    if (audioCtx && audioCtx.state === 'suspended') {
      audioCtx.resume();
    }
  }

  function playTone(freq, durationMs, type = 'sine') {
    initAudio();
    if (!audioCtx) return;
    try {
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = type;
      osc.frequency.setValueAtTime(freq, audioCtx.currentTime);
      gain.gain.setValueAtTime(0.15, audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + durationMs / 1000);
      osc.connect(gain);
      gain.connect(audioCtx.destination);
      osc.start();
      osc.stop(audioCtx.currentTime + durationMs / 1000);
    } catch (e) {
      console.warn("Audio playback error:", e);
    }
  }

  function playMarkerBeep() {
    playTone(950, 90, 'sine');
  }

  function playWarningTone() {
    playTone(400, 200, 'square');
    setTimeout(() => playTone(300, 250, 'square'), 220);
  }

  // --- Screen Wake Lock API ---
  let wakeLock = null;
  const btnWakeLock = document.getElementById('btn-wake-lock');

  async function requestWakeLock() {
    if ('wakeLock' in navigator) {
      try {
        wakeLock = await navigator.wakeLock.request('screen');
        btnWakeLock.classList.add('active');
        btnWakeLock.textContent = '📱 Screen Awake';
        wakeLock.addEventListener('release', () => {
          btnWakeLock.classList.remove('active');
          btnWakeLock.textContent = '📱 Wake Lock';
          wakeLock = null;
        });
      } catch (err) {
        console.warn('Wake Lock request failed:', err);
      }
    } else {
      btnWakeLock.style.display = 'none';
    }
  }

  btnWakeLock.addEventListener('click', () => {
    initAudio();
    if (wakeLock) {
      wakeLock.release();
      wakeLock = null;
    } else {
      requestWakeLock();
    }
  });

  // Re-acquire wake lock if tab regains visibility
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && !wakeLock && btnWakeLock.classList.contains('active')) {
      requestWakeLock();
    }
  });

  // --- UI Elements ---
  const statusPill = document.getElementById('status-pill');
  const statusText = document.getElementById('status-text');
  const alertBanner = document.getElementById('alert-banner');
  const alertMessage = document.getElementById('alert-message');

  const btnConnect = document.getElementById('btn-connect');
  const btnDisconnect = document.getElementById('btn-disconnect');
  const chkAutoconnect = document.getElementById('chk-autoconnect');
  const btnSimDrop = document.getElementById('btn-sim-drop');

  const sessionNameInput = document.getElementById('session-name');
  const btnStartRec = document.getElementById('btn-start-rec');
  const btnStopRec = document.getElementById('btn-stop-rec');
  const recElapsed = document.getElementById('rec-elapsed');
  const recCycles = document.getElementById('rec-cycles');
  const recSize = document.getElementById('rec-size');

  const chkAutostart = document.getElementById('chk-autostart');
  const chkKeeparmed = document.getElementById('chk-keeparmed');
  const chkAutostop = document.getElementById('chk-autostop');

  const valSpeed = document.getElementById('val-speed');
  const valRpm = document.getElementById('val-rpm');
  const rpmBox = document.querySelector('.rpm-box');
  const valPedal = document.getElementById('val-pedal');
  const valLoad = document.getElementById('val-load');
  const valCoolant = document.getElementById('val-coolant');
  const valVoltage = document.getElementById('val-voltage');

  const healthHz = document.getElementById('health-hz');
  const healthAge = document.getElementById('health-age');
  const healthErrors = document.getElementById('health-errors');
  const healthReconnects = document.getElementById('health-reconnects');

  const lastMarkerText = document.getElementById('last-marker-text');
  const lastMarkerAgo = document.getElementById('last-marker-ago');
  const btnUndoMarker = document.getElementById('btn-undo-marker');
  const badgeMode = document.getElementById('badge-mode');

  // --- State Variables ---
  let ws = null;
  let currentState = 'Disconnected';
  let isRecording = false;
  let lastMarkerTimestamp = null;
  let lastReadingWallTime = Date.now();
  let chartHistory = []; // {t: mono, speed: x, rpm: y}
  const MAX_HISTORY_S = 60.0;

  // Restore saved options
  chkAutoconnect.checked = localStorage.getItem('fleetpulse_autoconnect') === 'true';
  chkAutostart.checked = localStorage.getItem('fleetpulse_autostart') === 'true';
  chkKeeparmed.checked = localStorage.getItem('fleetpulse_keeparmed') === 'true';
  chkAutostop.checked = localStorage.getItem('fleetpulse_autostop') === 'true';

  chkAutoconnect.addEventListener('change', () => {
    localStorage.setItem('fleetpulse_autoconnect', chkAutoconnect.checked);
  });
  chkAutostart.addEventListener('change', () => {
    localStorage.setItem('fleetpulse_autostart', chkAutostart.checked);
  });
  chkKeeparmed.addEventListener('change', () => {
    localStorage.setItem('fleetpulse_keeparmed', chkKeeparmed.checked);
  });
  chkAutostop.addEventListener('change', () => {
    localStorage.setItem('fleetpulse_autostop', chkAutostop.checked);
  });

  // --- Status Pill Helper ---
  function updateConnectionState(stateStr) {
    currentState = stateStr;
    statusText.textContent = stateStr;
    statusPill.className = 'status-pill ' + stateStr.toLowerCase().replace(/\s+/g, '_');

    if (stateStr === 'Connected') {
      alertBanner.classList.add('hidden');
      btnConnect.disabled = true;
      btnDisconnect.disabled = false;
    } else if (stateStr === 'Reconnecting' || stateStr === 'Disconnected' || stateStr === 'ECU not responding') {
      btnConnect.disabled = (stateStr === 'Connecting' || stateStr === 'Waking adapter');
      btnDisconnect.disabled = (stateStr === 'Disconnected');

      if (stateStr === 'Reconnecting') {
        alertMessage.textContent = '⚠️ Bluetooth Connection Lost — Reconnecting...';
        alertBanner.classList.remove('hidden');
        playWarningTone();
      } else if (stateStr === 'ECU not responding') {
        alertMessage.textContent = '⚠️ ECU not responding (Ignition Off?)';
        alertBanner.classList.remove('hidden');
      } else {
        alertBanner.classList.add('hidden');
      }
    } else {
      btnConnect.disabled = true;
      btnDisconnect.disabled = false;
      alertBanner.classList.add('hidden');
    }
  }

  // --- WebSocket Setup ---
  function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      console.log('WebSocket connected');
      if (chkAutoconnect.checked && currentState === 'Disconnected') {
        fetch('/api/connect', { method: 'POST' }).catch(() => {});
      }
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        handleMessage(msg);
      } catch (err) {
        console.error('Error parsing WS message:', err);
      }
    };

    ws.onclose = () => {
      console.warn('WebSocket dropped. Reconnecting in 1.5s...');
      setTimeout(connectWebSocket, 1500);
    };

    ws.onerror = (err) => {
      console.warn('WebSocket error:', err);
      ws.close();
    };
  }

  function handleMessage(msg) {
    if (msg.type === 'init') {
      updateConnectionState(msg.connection.state);
      updateRecorderStats(msg.recorder);
      if (msg.is_mock) {
        badgeMode.textContent = 'MOCK MODE';
        badgeMode.style.color = '#38bdf8';
        btnSimDrop.classList.remove('hidden');
      }
    } else if (msg.type === 'state_change') {
      updateConnectionState(msg.new_state);
      healthReconnects.textContent = msg.reconnect_count || 0;
      healthErrors.textContent = msg.error_count || 0;
    } else if (msg.type === 'telemetry') {
      renderTelemetry(msg.data);
      updateRecorderStats(msg.recorder);
      updateHealth(msg);
    } else if (msg.type === 'marker_added') {
      setLastMarker(msg.marker);
    } else if (msg.type === 'marker_undone') {
      lastMarkerText.textContent = 'Marker undone';
      lastMarkerAgo.textContent = '';
    }
  }

  // --- Render Telemetry ---
  function renderTelemetry(d) {
    lastReadingWallTime = Date.now();

    const speed = Math.floor(d.speed_kmh || 0);
    const rpm = Math.floor(d.rpm || 0);

    valSpeed.textContent = speed;
    valRpm.textContent = rpm;

    if (rpm > 3800) {
      rpmBox.classList.add('redline');
    } else {
      rpmBox.classList.remove('redline');
    }

    valPedal.textContent = (d.accel_pedal_pct || 0).toFixed(1) + '%';
    valLoad.textContent = (d.engine_load_pct || 0).toFixed(1) + '%';

    if (d.coolant_temp_c !== '' && d.coolant_temp_c !== undefined) {
      valCoolant.textContent = d.coolant_temp_c + '°C';
    }
    if (d.module_voltage_v !== '' && d.module_voltage_v !== undefined) {
      valVoltage.textContent = parseFloat(d.module_voltage_v).toFixed(2) + 'V';
    }

    // Add to canvas chart history
    chartHistory.push({ t: d.t_mono, speed, rpm });
    const cutoff = d.t_mono - MAX_HISTORY_S;
    while (chartHistory.length > 0 && chartHistory[0].t < cutoff) {
      chartHistory.shift();
    }
    drawRollingChart();
  }

  function updateRecorderStats(rec) {
    if (!rec) return;
    isRecording = rec.is_recording;
    btnStartRec.disabled = isRecording;
    btnStopRec.disabled = !isRecording;

    if (isRecording) {
      const s = Math.floor(rec.elapsed_s || 0);
      const hrs = String(Math.floor(s / 3600)).padStart(2, '0');
      const mins = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
      const secs = String(s % 60).padStart(2, '0');
      recElapsed.textContent = `${hrs}:${mins}:${secs}`;
      recCycles.textContent = rec.total_cycles;
      recSize.textContent = (rec.file_size_kb || 0).toFixed(1) + ' KB';
    } else {
      recElapsed.textContent = '00:00:00';
    }

    if (rec.last_marker && !lastMarkerTimestamp) {
      setLastMarker(rec.last_marker);
    }
  }

  function updateHealth(msg) {
    const hz = msg.recorder?.hz || msg.benchmark?.achieved_fast_hz || 0;
    healthHz.textContent = hz.toFixed(1) + ' Hz';

    const ageS = ((Date.now() - lastReadingWallTime) / 1000).toFixed(1);
    healthAge.textContent = ageS + 's';
    if (ageS > 2.0) {
      healthAge.classList.add('warning');
    } else {
      healthAge.classList.remove('warning');
    }

    if (msg.connection) {
      healthErrors.textContent = msg.connection.error_count || 0;
      healthReconnects.textContent = msg.connection.reconnect_count || 0;
    }
  }

  // --- Rolling Canvas Chart (Zero Libraries) ---
  const canvas = document.getElementById('rolling-chart');
  const ctx = canvas.getContext('2d');

  function resizeCanvas() {
    const rect = canvas.getBoundingClientRect();
    if (rect.width > 0 && (canvas.width !== rect.width || canvas.height !== rect.height)) {
      canvas.width = rect.width;
      canvas.height = rect.height;
    }
  }
  window.addEventListener('resize', resizeCanvas);
  setTimeout(resizeCanvas, 100);

  function drawRollingChart() {
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);

    if (chartHistory.length < 2) return;

    const tMax = chartHistory[chartHistory.length - 1].t;
    const tMin = tMax - MAX_HISTORY_S;

    // Draw grid lines
    ctx.strokeStyle = '#1e293b';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, h * 0.5); ctx.lineTo(w, h * 0.5);
    ctx.stroke();

    // 1. Draw Speed line (Cyan, max 120 km/h)
    ctx.strokeStyle = '#06b6d4';
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (let i = 0; i < chartHistory.length; i++) {
      const pt = chartHistory[i];
      const x = ((pt.t - tMin) / MAX_HISTORY_S) * w;
      const y = h - (Math.min(120, pt.speed) / 120.0) * (h - 6) - 3;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();

    // 2. Draw RPM line (Amber, max 5000 RPM)
    ctx.strokeStyle = '#f59e0b';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    for (let i = 0; i < chartHistory.length; i++) {
      const pt = chartHistory[i];
      const x = ((pt.t - tMin) / MAX_HISTORY_S) * w;
      const y = h - (Math.min(5000, pt.rpm) / 5000.0) * (h - 6) - 3;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }

  // --- Marker Actions ---
  function triggerMarker(label, keyNum) {
    initAudio();
    playMarkerBeep();

    let note = '';
    if (label === 'OTHER') {
      note = prompt('Event Note (optional):', '') || '';
    }

    // Send via REST
    fetch('/api/markers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label, note })
    }).catch(err => console.error("Error posting marker:", err));
  }

  function setLastMarker(marker) {
    if (!marker) return;
    lastMarkerTimestamp = Date.now();
    lastMarkerText.textContent = `${marker.label}${marker.note ? ' (' + marker.note + ')' : ''}`;
    updateMarkerAgoTimer();
  }

  function updateMarkerAgoTimer() {
    if (!lastMarkerTimestamp) return;
    const agoSec = Math.floor((Date.now() - lastMarkerTimestamp) / 1000);
    lastMarkerAgo.textContent = `${agoSec}s ago`;
  }
  setInterval(updateMarkerAgoTimer, 1000);

  // Wire marker buttons
  document.querySelectorAll('.btn-marker').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const label = btn.getAttribute('data-label');
      const key = btn.getAttribute('data-key');
      triggerMarker(label, key);
    });
  });

  // Undo Last Marker
  btnUndoMarker.addEventListener('click', () => {
    initAudio();
    playTone(500, 80, 'sine');
    fetch('/api/markers/undo', { method: 'POST' }).catch(err => alert("No markers to undo"));
  });

  // Keyboard Shortcuts (1-6)
  window.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT') return;
    const key = e.key;
    const btn = document.querySelector(`.btn-marker[data-key="${key}"]`);
    if (btn) {
      btn.click();
    }
  });

  // --- Control Buttons ---
  btnConnect.addEventListener('click', () => {
    initAudio();
    fetch('/api/connect', { method: 'POST' });
  });

  btnDisconnect.addEventListener('click', () => {
    initAudio();
    fetch('/api/disconnect', { method: 'POST' });
  });

  btnStartRec.addEventListener('click', () => {
    initAudio();
    playTone(700, 100, 'sine');
    const name = sessionNameInput.value.trim() || 'drive';
    fetch('/api/recording/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name })
    });
  });

  btnStopRec.addEventListener('click', () => {
    initAudio();
    playTone(450, 150, 'sine');
    fetch('/api/recording/stop', { method: 'POST' });
  });

  btnSimDrop.addEventListener('click', () => {
    fetch('/api/mock/disconnect', { method: 'POST' });
  });

  // Start WebSocket on boot
  connectWebSocket();

})();
