/* ===========================================================================
   THE BLACKWALL
   ---------------------------------------------------------------------------
   A single WebGL pass, built from four layers stacked back to front:

     1. RAIN   - vertical filaments of data falling the full height of the
                 frame. Sparse and near-black at rest; dense and lit when the
                 wall is active. This is what makes it read as a *wall* rather
                 than as an audio visualiser.
     2. BEAM   - a soft vertical column of light up the centre.
     3. BAND   - the horizontal energy seam. Thin at the edges of the frame,
                 swelling to a bright mass in the middle, torn into displaced
                 blocks as it gets louder.
     4. GRADE  - chromatic split, tearing, scanlines, grain, vignette, and a
                 filmic rolloff so the core saturates to white-pink instead of
                 clipping to flat red.

   Everything is driven by two numbers: `energy` (a smoothed activation, 0..1,
   chosen by the state machine) and `level` (live audio amplitude). At rest
   energy is near zero and all of it collapses to one stationary red line.
   =========================================================================== */

(function (global) {
  'use strict';

  const VERT = `
    attribute vec2 a_pos;
    void main() { gl_Position = vec4(a_pos, 0.0, 1.0); }
  `;

  const FRAG = `
    precision highp float;

    uniform vec2  u_res;
    uniform float u_time;
    uniform float u_energy;     // smoothed activation 0..1
    uniform float u_level;      // instantaneous amplitude 0..1
    uniform float u_state;      // 0 idle 1 listening 2 thinking 3 speaking 4 error
    uniform float u_pulse;      // 1 -> 0 decay, fires when an action runs
    uniform float u_intensity;
    uniform float u_grain;
    uniform float u_scan;
    uniform sampler2D u_wave;   // 256x1, R = recent amplitude history

    /* -- hashing & noise --------------------------------------------------- */

    float hash11(float p) {
      p = fract(p * 0.1031);
      p *= p + 33.33;
      p *= p + p;
      return fract(p);
    }

    float hash21(vec2 p) {
      p = fract(p * vec2(123.34, 456.21));
      p += dot(p, p + 45.32);
      return fract(p.x * p.y);
    }

    float vnoise(vec2 p) {
      vec2 i = floor(p), f = fract(p);
      f = f * f * (3.0 - 2.0 * f);
      float a = hash21(i);
      float b = hash21(i + vec2(1.0, 0.0));
      float c = hash21(i + vec2(0.0, 1.0));
      float d = hash21(i + vec2(1.0, 1.0));
      return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
    }

    float fbm(vec2 p) {
      float v = 0.0, a = 0.5;
      for (int i = 0; i < 4; i++) { v += a * vnoise(p); p *= 2.03; a *= 0.5; }
      return v;
    }

    // GLSL ES 1.00 predates tanh.
    float softclip(float x) {
      float e2 = exp(2.0 * clamp(x, -8.0, 8.0));
      return (e2 - 1.0) / (e2 + 1.0);
    }

    /* -- 1. vertical data rain --------------------------------------------- */

    /* One layer of filaments. 'cols' sets how many vertical lanes exist across
       the frame; stacking two or three layers at different densities is what
       gives the field depth instead of looking like a comb. */
    float rain(vec2 uv, float t, float cols, float seed) {
      float x  = uv.x * cols + seed * 31.0;
      float c  = floor(x);
      float fx = abs(fract(x) - 0.5) * 2.0;

      // A hard, thin filament down the middle of its lane.
      float thin = pow(max(0.0, 1.0 - fx), 16.0);

      float h  = hash11(c * 1.13 + seed);
      float on = step(0.40, h);                       // most lanes stay dark

      // Slow flicker, each lane on its own clock.
      float flick = 0.62 + 0.38 * sin(t * (0.45 + h * 2.1) + h * 37.0);

      // Break each lane into glowing runs of varying length, drifting downward
      // very slowly - fast enough to be alive, slow enough not to read as rain
      // in a videogame sense.
      float ys  = uv.y * (1.2 + h * 3.4) + h * 11.0 - t * 0.035;
      float seg = pow(vnoise(vec2(c * 0.61 + seed, ys)), 3.0);

      return thin * seg * flick * on;
    }

    /* -- 3. the horizontal band -------------------------------------------- */

    float waveAt(float x) {
      return texture2D(u_wave, vec2(clamp(x * 0.5 + 0.5, 0.0, 1.0), 0.5)).r * 2.0 - 1.0;
    }

    /* Where the seam sits at a given x. Deliberately restrained - the band is
       broadly horizontal and only breathes; it is the brightness envelope, not
       the displacement, that carries the drama. */
    float wallY(float x, float t, float e) {
      float slow = fbm(vec2(x * 1.05, t * 0.24)) - 0.5;
      float mid  = fbm(vec2(x * 4.40 + 13.0, t * 0.78)) - 0.5;
      float y = (slow * 0.58 + mid * 0.30) * e + waveAt(x) * 0.22 * e;
      return softclip(y * 2.2) * 0.30;
    }

    /* Brightness envelope along x: a thin line out at the edges of the frame,
       swelling into a mass at the centre. This single curve is most of why the
       wall reads the way it does. */
    float envelope(float x) {
      return 0.11 + 0.89 * exp(-x * x * 0.52);
    }

    struct Band { float field; float mask; };

    Band bandAt(vec2 p, float t, float e, float xoff) {
      float x = p.x + xoff;
      float env = envelope(x);
      float d = abs(p.y - wallY(x, t, e));

      // Hard bright core. At e = 0 this is the stationary idle line.
      float coreW = 0.0015 + 0.028 * e * env;
      float core  = pow(coreW / (d + coreW), 1.80);

      // Bloom, scaled by the envelope so it only blows out near the middle.
      float haloW = 0.006 + (0.014 + 0.32 * e) * env;
      float halo  = pow(haloW / (d + haloW), 2.60) * 0.62;

      // Everything else in the band is confined to this mask. Keeping it
      // tight is what preserves the black around the seam.
      float mask = exp(-(d * d) / (0.0020 + 0.030 * e * env));

      // Torn blocks: quantise into rows, then into runs along x, and light a
      // fraction of them. Re-rolled a few times a second.
      float clock = floor(t * 7.0);
      float row   = floor(p.y * 150.0);
      float rseed = hash21(vec2(row, clock));
      float bx    = floor(x * (3.0 + rseed * 12.0) + hash21(vec2(row, 3.0)) * 30.0);
      float blocks = step(0.70, hash21(vec2(bx, row + clock * 0.37)))
                     * mask * e * env * 1.35;

      // Horizontal smear streaks - short bright dashes along the seam.
      // Higher power = more black between the dashes, which is what makes
      // them read as torn data rather than as a smooth glow.
      float smear = pow(vnoise(vec2(x * 7.0 + t * 0.5, row * 0.4)), 6.0)
                    * mask * e * env * 2.6;

      // Travelling nodes when an action fires.
      float px   = (1.0 - u_pulse) * 2.4;
      float node = exp(-pow((abs(x) - px) / 0.05, 2.0)) * u_pulse * mask * 1.8;

      Band b;
      b.field = core + halo + blocks + smear + node;
      b.mask  = mask;
      return b;
    }

    /* -- palette ------------------------------------------------------------ */

    vec3 tint(float v, float err) {
      vec3 deep = vec3(0.26, 0.000, 0.035);   // near-black maroon
      vec3 red  = vec3(1.00, 0.085, 0.160);   // the wall's red, pushed pink
      vec3 hot  = vec3(1.00, 0.800, 0.860);   // white-pink core
      vec3 col = mix(deep, red, clamp(v * 1.45, 0.0, 1.0));
      col = mix(col, hot, clamp((v - 0.85) * 1.70, 0.0, 1.0));
      return mix(col, vec3(dot(col, vec3(0.3333))), err * 0.75);
    }

    void main() {
      vec2 uv = gl_FragCoord.xy / u_res;
      vec2 p  = uv * 2.0 - 1.0;
      p.x *= u_res.x / u_res.y;

      float t   = u_time;
      float e   = u_energy;
      float err = step(3.5, u_state);

      /* -- tearing: displace thin bands of scanlines, rarely and raggedly --- */
      float trow  = floor(uv.y * 200.0);
      float tclk  = floor(t * 9.0);
      float tearOn = step(0.992 - 0.020 * e - 0.20 * err, hash21(vec2(trow, tclk)));
      tearOn *= max(step(0.986, hash21(vec2(floor(trow / 3.0), tclk))), 0.35);
      float shift = (hash21(vec2(trow, tclk + 7.0)) - 0.5) * 0.26 * tearOn * (e + err);
      p.x  += shift;
      uv.x += shift * 0.5;

      /* -- 1. rain ---------------------------------------------------------- */
      float r = rain(uv, t, 300.0, 1.0) * 1.00
              + rain(uv, t, 150.0, 5.0) * 0.78
              + rain(uv, t,  72.0, 9.0) * 0.55
              + rain(uv, t,  34.0, 17.0) * 0.40;   // a few heavy trunk lines
      // Thicker toward the sides, so the centre stays legible.
      r *= 0.55 + 0.75 * smoothstep(0.0, 1.5, abs(p.x));
      r *= 0.04 + 0.96 * e;          // at rest the field all but disappears
      r *= 2.6;

      /* -- 2. central beam --------------------------------------------------- */
      float beam = exp(-(p.x * p.x) / (0.011 + 0.055 * e)) * (0.06 + 0.55 * e);

      /* -- 3. band, sampled three times for chromatic split ------------------ */
      float ca = (0.0016 + 0.011 * e + 0.020 * err) * (0.6 + 0.4 * u_level);
      Band br = bandAt(p, t, e, -ca);
      Band bg = bandAt(p, t, e,  0.0);
      Band bb = bandAt(p, t, e,  ca);

      vec3 col = vec3(0.0);
      col += tint(br.field, err) * vec3(1.20, 0.28, 0.34) * br.field;
      col += tint(bg.field, err) * vec3(0.86, 0.48, 0.50) * bg.field;
      col += tint(bb.field, err) * vec3(0.70, 0.30, 0.92) * bb.field;
      col *= 0.60 * u_intensity;

      /* -- 4. composite ------------------------------------------------------ */

      // Rain is deep red on its own, but picks up heat where it crosses the seam.
      col += vec3(0.95, 0.055, 0.13) * r * 0.85 * u_intensity;
      col += vec3(1.00, 0.560, 0.60) * r * bg.mask * e * 0.70;

      col += vec3(0.85, 0.045, 0.11) * beam * 0.38 * u_intensity;

      // The seam lights the space above and below it.
      float wash = exp(-abs(p.y) * (4.2 - 1.6 * e)) * (0.014 + 0.055 * e);
      col += vec3(0.55, 0.02, 0.05) * wash * envelope(p.x) * 1.2;

      float scan = 1.0 - u_scan * 0.13 *
                   (0.5 + 0.5 * sin(gl_FragCoord.y * 1.85 + t * 1.2));
      col *= scan;

      float g = hash21(gl_FragCoord.xy + fract(t) * 137.0) - 0.5;
      col += g * u_grain * (0.35 + 0.65 * length(col));

      col *= 1.0 - 0.55 * pow(length(p * vec2(0.40, 0.76)), 2.4);

      col = col / (col + vec3(0.85));
      col = pow(col, vec3(0.82));

      gl_FragColor = vec4(max(col, vec3(0.0)), 1.0);
    }
  `;

  /** Target energy per state. This table is the entire "look" design. */
  const ENERGY = {
    idle:      0.012,   // one stationary line; rain and beam all but gone
    listening: 0.32,    // field wakes up, seam reactive but coherent
    thinking:  0.52,    // churning, no voice driving it
    speaking:  0.80,    // full wall, driven by the outgoing audio
    error:     0.45
  };

  const STATE_ID = { idle: 0, listening: 1, thinking: 2, speaking: 3, error: 4 };

  const WAVE_SIZE = 256;

  class Blackwall {
    constructor(canvas, options) {
      this.canvas = canvas;
      this.options = Object.assign(
        { intensity: 1.0, grain: 0.08, scanlines: true }, options || {}
      );

      this.state = 'idle';
      this.energy = ENERGY.idle;
      this.level = 0;
      this.levelSmoothed = 0;
      this.pulse = 0;
      this.startedAt = performance.now();

      // Ring buffer of recent amplitudes, uploaded each frame as a 1D texture.
      this.wave = new Uint8Array(WAVE_SIZE).fill(128);
      this.waveHead = 0;
      this.unrolled = new Uint8Array(WAVE_SIZE);

      this.gl = canvas.getContext('webgl', {
        antialias: false, alpha: false, powerPreference: 'high-performance'
      });
      if (!this.gl) { this._fallback(); return; }

      this._initGL();
      this._resize();
      window.addEventListener('resize', () => this._resize());
      this._frame = this._frame.bind(this);
      requestAnimationFrame(this._frame);
    }

    /* -- public API ------------------------------------------------------- */

    setState(state) {
      if (!(state in ENERGY)) return;
      this.state = state;
      // Let the line settle flat rather than decaying through a wobble.
      if (state === 'idle') this.wave.fill(128);
    }

    /** Feed one amplitude sample (0..1), at whatever rate audio arrives. */
    setLevel(value) {
      this.level = Math.max(0, Math.min(1, value * 3.2));
    }

    /** Fire the travelling-node pulse - an action just executed. */
    ping() { this.pulse = 1.0; }

    /* -- internals -------------------------------------------------------- */

    _fallback() {
      // No WebGL (ancient browser, or a locked-down webview): draw the idle
      // line in CSS so the UI still reads correctly.
      this.canvas.style.background =
        'radial-gradient(ellipse 100% 3px at 50% 50%, #ff2438 0%, ' +
        'rgba(255,36,56,0.35) 30%, transparent 72%), #050203';
      console.warn('[blackwall] WebGL unavailable - static fallback');
    }

    _compile(type, source) {
      const gl = this.gl;
      const shader = gl.createShader(type);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        console.error('[blackwall] shader:', gl.getShaderInfoLog(shader));
      }
      return shader;
    }

    _initGL() {
      const gl = this.gl;
      const program = gl.createProgram();
      gl.attachShader(program, this._compile(gl.VERTEX_SHADER, VERT));
      gl.attachShader(program, this._compile(gl.FRAGMENT_SHADER, FRAG));
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
        console.error('[blackwall] link:', gl.getProgramInfoLog(program));
      }
      gl.useProgram(program);
      this.program = program;

      const buffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(
        gl.ARRAY_BUFFER,
        new Float32Array([-1, -1, 3, -1, -1, 3]),   // one oversized triangle
        gl.STATIC_DRAW
      );
      const loc = gl.getAttribLocation(program, 'a_pos');
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);

      this.u = {};
      for (const name of ['u_res', 'u_time', 'u_energy', 'u_level', 'u_state',
                          'u_pulse', 'u_intensity', 'u_grain', 'u_scan', 'u_wave']) {
        this.u[name] = gl.getUniformLocation(program, name);
      }

      this.waveTex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, this.waveTex);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    }

    _resize() {
      // Cap DPR: a 4K wall at 3x costs a lot of fill rate for no visible gain.
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = Math.floor(this.canvas.clientWidth * dpr);
      const h = Math.floor(this.canvas.clientHeight * dpr);
      if (this.canvas.width === w && this.canvas.height === h) return;
      this.canvas.width = w;
      this.canvas.height = h;
      this.gl.viewport(0, 0, w, h);
    }

    _pushWave() {
      this.wave[this.waveHead] = Math.floor(
        128 + this.levelSmoothed * 127 * (Math.random() * 0.4 + 0.8)
      );
      this.waveHead = (this.waveHead + 1) % WAVE_SIZE;

      // Unroll so index 0 is the oldest sample; the seam then scrolls in time.
      for (let i = 0; i < WAVE_SIZE; i++) {
        this.unrolled[i] = this.wave[(this.waveHead + i) % WAVE_SIZE];
      }

      const gl = this.gl;
      gl.bindTexture(gl.TEXTURE_2D, this.waveTex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.LUMINANCE, WAVE_SIZE, 1, 0,
                    gl.LUMINANCE, gl.UNSIGNED_BYTE, this.unrolled);
    }

    _frame(now) {
      requestAnimationFrame(this._frame);
      const gl = this.gl;
      if (!gl) return;

      const t = (now - this.startedAt) / 1000;

      // Ease toward the target. Rising fast and falling slow is what makes it
      // feel like something waking up rather than a value being set.
      const target = ENERGY[this.state];
      this.energy += (target - this.energy) * (target > this.energy ? 0.14 : 0.045);

      this.levelSmoothed += (this.level - this.levelSmoothed) *
                            (this.level > this.levelSmoothed ? 0.45 : 0.09);
      this.level *= 0.92;    // decay if nothing new arrives

      this.pulse *= 0.94;
      if (this.pulse < 0.01) this.pulse = 0;

      this._pushWave();

      const active = this.state === 'listening' || this.state === 'speaking';
      const energy = Math.min(1.0,
        this.energy + (active ? this.levelSmoothed * 0.30 : 0));

      gl.uniform2f(this.u.u_res, this.canvas.width, this.canvas.height);
      gl.uniform1f(this.u.u_time, t);
      gl.uniform1f(this.u.u_energy, energy);
      gl.uniform1f(this.u.u_level, this.levelSmoothed);
      gl.uniform1f(this.u.u_state, STATE_ID[this.state] || 0);
      gl.uniform1f(this.u.u_pulse, this.pulse);
      gl.uniform1f(this.u.u_intensity, this.options.intensity);
      gl.uniform1f(this.u.u_grain, this.options.grain);
      gl.uniform1f(this.u.u_scan, this.options.scanlines ? 1.0 : 0.0);

      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, this.waveTex);
      gl.uniform1i(this.u.u_wave, 0);

      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }
  }

  global.Blackwall = Blackwall;
})(window);
