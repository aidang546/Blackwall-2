/* ===========================================================================
   EREBUS - client.
   Connects to the daemon, drives the wall from its state, and offers two ways
   in besides the wake word: a text console and push-to-talk.

   Push-to-talk has two modes. On the desktop machine the daemon owns the
   microphone, so the button just tells it to record. On any other device (your
   phone) the daemon's microphone is in the wrong room, so the browser captures
   locally: Web Speech API for the transcript, and an AnalyserNode so the wall
   still reacts to your actual voice while you hold the button.
   =========================================================================== */

(function () {
  'use strict';

  const el = {
    wall:       document.getElementById('wall'),
    status:     document.getElementById('status'),
    dot:        document.getElementById('dot'),
    transcript: document.getElementById('transcript'),
    reply:      document.getElementById('reply'),
    input:      document.getElementById('input'),
    console:    document.getElementById('console'),
    talk:       document.getElementById('talk'),
    wakeHint:   document.getElementById('wake-hint'),
    caps:       document.getElementById('capabilities')
  };

  // A token in the URL fragment is how a phone pairs: the desktop prints a URL
  // with #token=... and a fragment is never sent to the server or leaked in a
  // referrer. We store it and strip it from the address bar.
  //
  // localStorage, not sessionStorage: installed to a home screen the app starts
  // at the manifest's start_url with no fragment, and a session store would
  // unpair the phone on every launch. Private browsing can throw on both, hence
  // the guards - an unpaired client fails with a clear message rather than a
  // stack trace.
  function readToken() {
    for (const store of [localStorage, sessionStorage]) {
      try {
        const value = store.getItem('erebus_token');
        if (value) return value;
      } catch (e) { /* storage disabled */ }
    }
    return '';
  }

  function saveToken(value) {
    for (const store of [localStorage, sessionStorage]) {
      try { store.setItem('erebus_token', value); return; } catch (e) { /* next */ }
    }
  }

  const params = new URLSearchParams(location.hash.slice(1));
  if (params.get('token')) {
    saveToken(params.get('token'));
    history.replaceState(null, '', location.pathname);
  }
  let token = readToken();

  const wall = new Blackwall(el.wall);

  // Exposed on purpose: tuning the wall's look is much easier from the console
  // (`__wall.setState('speaking')`) than by restarting the daemon each time.
  window.__wall = wall;

  let socket = null;
  let reconnectDelay = 500;
  let replyTimer = null;

  /* -- connection --------------------------------------------------------- */

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const query = token ? '?token=' + encodeURIComponent(token) : '';
    socket = new WebSocket(proto + '://' + location.host + '/ws' + query);

    socket.onopen = function () {
      reconnectDelay = 500;
      setStatus('idle', 'live');
    };

    socket.onmessage = function (event) {
      let msg;
      try { msg = JSON.parse(event.data); } catch (e) { return; }
      handle(msg);
    };

    socket.onclose = function (event) {
      if (event.code === 4403) {
        setStatus('unauthorized', 'error');
        show(el.reply, 'Not paired. Open the link the desktop printed.');
        return;   // a bad token will not fix itself by retrying
      }
      setStatus('reconnecting', 'error');
      // Back off, but stay responsive if the daemon is just restarting.
      setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 1.7, 8000);
    };

    socket.onerror = function () { /* onclose handles it */ };
  }

  function send(payload) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(payload));
      return true;
    }
    return false;
  }

  /* -- inbound events ----------------------------------------------------- */

  function handle(msg) {
    switch (msg.kind) {
      case 'hello':
        applyConfig(msg);
        wall.setState(msg.state);
        setStatus(msg.state, msg.state === 'idle' ? 'live' : msg.state);
        break;

      case 'state':
        wall.setState(msg.state);
        setStatus(msg.state, msg.state === 'idle' ? 'live'
                           : msg.state === 'error' ? 'error' : msg.state);
        if (msg.state === 'listening') { hide(el.reply); hide(el.transcript); }
        break;

      case 'level':
        wall.setLevel(msg.value);
        break;

      case 'wake':
        hide(el.reply);
        break;

      case 'transcript':
        show(el.transcript, msg.text);
        break;

      case 'reply':
        show(el.reply, msg.text);
        break;

      case 'action':
        wall.ping();
        show(el.transcript, (msg.name || '').replace(/_/g, ' ').toUpperCase());
        break;

      case 'done':
        wall.ping();
        break;

      case 'notice':
        show(el.reply, msg.text);
        break;

      case 'capabilities':
        renderCapabilities(msg);
        break;
    }
  }

  function applyConfig(msg) {
    if (msg.ui) {
      if (typeof msg.ui.intensity === 'number') wall.options.intensity = msg.ui.intensity;
      if (typeof msg.ui.grain === 'number')     wall.options.grain = msg.ui.grain;
      if (typeof msg.ui.scanlines === 'boolean') wall.options.scanlines = msg.ui.scanlines;
      if (msg.ui.show_transcript === false) el.transcript.style.display = 'none';
    }
    if (msg.name) document.title = String(msg.name).toUpperCase();
  }

  function renderCapabilities(caps) {
    const rows = [
      ['wake word', caps.wake],
      ['speech in', caps.stt],
      ['speech out', caps.tts],
      ['reasoning', caps.brain]
    ];
    el.caps.innerHTML = rows.map(function (row) {
      return '<div class="' + (row[1] ? 'on' : 'off') + '">' + row[0] + '</div>';
    }).join('');
    if (!caps.wake) el.wakeHint.textContent = 'push to talk';
  }

  /* -- HUD ---------------------------------------------------------------- */

  function setStatus(text, tone) {
    el.status.textContent = text;
    el.dot.className = 'status-dot ' + (tone || '');
  }

  function show(node, text) {
    node.textContent = text;
    node.classList.add('show');
    if (node === el.reply) {
      clearTimeout(replyTimer);
      // Long replies need longer on screen; everything fades eventually so the
      // wall is never permanently occluded by stale text.
      replyTimer = setTimeout(function () { hide(node); },
                              4000 + text.length * 55);
    }
  }

  function hide(node) { node.classList.remove('show'); }

  /* -- text console -------------------------------------------------------- */

  el.console.addEventListener('submit', function (event) {
    event.preventDefault();
    const text = el.input.value.trim();
    if (!text) return;
    if (send({ kind: 'text', text: text })) el.input.value = '';
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') {
      send({ kind: 'interrupt' });
      el.input.blur();
    }
    // Space anywhere outside the input is push-to-talk.
    if (event.code === 'Space' && document.activeElement !== el.input &&
        !event.repeat) {
      event.preventDefault();
      startTalking();
    }
  });

  document.addEventListener('keyup', function (event) {
    if (event.code === 'Space' && document.activeElement !== el.input) {
      stopTalking();
    }
  });

  /* -- push to talk --------------------------------------------------------- */

  // The daemon owns a microphone only on the machine it runs on. Anywhere else
  // we have to capture in the browser.
  const isLocalHost = ['localhost', '127.0.0.1', '[::1]'].indexOf(location.hostname) >= 0;

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  let recognition = null;
  let audioCtx = null;
  let analyser = null;
  let micStream = null;
  let levelRAF = 0;
  let talking = false;

  function startTalking() {
    if (talking) return;
    talking = true;
    el.talk.classList.add('active');
    hide(el.reply);

    if (isLocalHost) {
      send({ kind: 'ptt' });        // daemon records with the good microphone
      return;
    }
    startBrowserCapture();
  }

  function stopTalking() {
    if (!talking) return;
    talking = false;
    el.talk.classList.remove('active');
    if (isLocalHost) return;        // the daemon decides when you stopped
    stopBrowserCapture();
  }

  function startBrowserCapture() {
    // Drive the wall from this device's own microphone.
    if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
      navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
        micStream = stream;
        audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
        const source = audioCtx.createMediaStreamSource(stream);
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = 512;
        source.connect(analyser);
        pumpLevels();
      }).catch(function (err) {
        show(el.reply, 'Microphone blocked: ' + err.name);
      });
    }

    if (!SpeechRecognition) {
      show(el.reply, 'This browser has no speech recognition. Type instead.');
      return;
    }
    recognition = new SpeechRecognition();
    recognition.lang = 'en-US';
    recognition.interimResults = true;
    recognition.continuous = false;

    recognition.onresult = function (event) {
      let text = '';
      let isFinal = false;
      for (let i = event.resultIndex; i < event.results.length; i++) {
        text += event.results[i][0].transcript;
        if (event.results[i].isFinal) isFinal = true;
      }
      show(el.transcript, text);
      if (isFinal && text.trim()) send({ kind: 'text', text: text.trim() });
    };
    recognition.onerror = function (event) {
      if (event.error !== 'aborted' && event.error !== 'no-speech') {
        show(el.reply, 'Recognition error: ' + event.error);
      }
    };
    try { recognition.start(); } catch (e) { /* already running */ }
  }

  function stopBrowserCapture() {
    if (recognition) { try { recognition.stop(); } catch (e) {} recognition = null; }
    cancelAnimationFrame(levelRAF);
    if (micStream) {
      micStream.getTracks().forEach(function (track) { track.stop(); });
      micStream = null;
    }
    analyser = null;
    wall.setLevel(0);
  }

  function pumpLevels() {
    if (!analyser) return;
    const buffer = new Uint8Array(analyser.fftSize);
    (function tick() {
      if (!analyser) return;
      analyser.getByteTimeDomainData(buffer);
      let sum = 0;
      for (let i = 0; i < buffer.length; i++) {
        const v = (buffer[i] - 128) / 128;
        sum += v * v;
      }
      wall.setLevel(Math.sqrt(sum / buffer.length));
      levelRAF = requestAnimationFrame(tick);
    })();
  }

  el.talk.addEventListener('pointerdown', function (event) {
    event.preventDefault();
    startTalking();
  });
  ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (name) {
    el.talk.addEventListener(name, stopTalking);
  });

  // `?demo` cycles the states with synthetic audio - lets you tune the wall
  // with nothing else installed. `python -m erebus --no-voice` + this is the
  // whole visual workflow.
  if (new URLSearchParams(location.search).has('demo')) {
    const script = [
      ['idle', 0, 3500], ['listening', 0.30, 2600], ['thinking', 0, 2200],
      ['speaking', 0.70, 4200]
    ];
    let step = 0;
    setInterval(function () { wall.setLevel(Math.random() * (demoLevel || 0)); }, 40);
    var demoLevel = 0;
    (function next() {
      const [state, level, hold] = script[step % script.length];
      step++;
      wall.setState(state);
      demoLevel = level;
      setStatus(state, state === 'idle' ? 'live' : state);
      setTimeout(next, hold);
    })();
    return;
  }

  connect();
})();
