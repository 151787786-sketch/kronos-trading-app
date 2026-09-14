/*!
 * AeroShards — 零依赖 vanilla 移植版（WebGL）
 *
 * 原组件是 React 组件（<AeroShards ... />），本文件按它的每个参数语义 1:1 复刻为
 * 纯 JS + WebGL 的全屏背景，可直接在非 React 页面使用：
 *
 *   AeroShards.create(canvasEl, {
 *     backgroundColor: '#120F17', shardColor: '#896ABD', accentColor: '#A855F7',
 *     placement: 'full', flow: 'stream', material: 'pearl', detail: 'balanced',
 *     effect: 'none', scale: 1, spread: 1, depth: 1, speed: 1, spin: 1,
 *     interaction: 'repel', density: 1.5, shardSize: 1.1, stretch: 1,
 *     turbulence: 1, glow: 1, edgeSoftness: 2, bloom: 0.5, grain: 0.05,
 *     chromaticAberration: 0.0075, transitionDuration: 1,
 *     interactionRadius: 1.5, interactionStrength: 0.5,
 *     rippleIntensity: 1, holdToGather: true
 *   });
 *
 * 参数含义：
 *   backgroundColor      背景底色
 *   shardColor           碎片本体色
 *   accentColor          碎片高光/边缘辉光色
 *   placement            full | top | center —— 铺满 / 偏上 / 居中
 *   flow                 stream | drift | orbit —— 单向流动 / 斜向漂移 / 环绕
 *   material             pearl | glass | metal | matte —— 珍珠 / 玻璃 / 金属 / 哑光
 *   detail               low | balanced | high —— 邻居采样半径 1/2（影响细节与性能）
 *   effect               none | vignette —— 额外后期
 *   scale/spread/depth/speed/spin     整体尺寸/疏密/纵深/速度/自转强度
 *   density              碎片密度
 *   shardSize            碎片大小
 *   stretch              碎片拉长比例
 *   turbulence           湍流扰动强度
 *   glow                 碎片自身辉光
 *   edgeSoftness         边缘柔化（越大越糊）
 *   bloom                泛光强度
 *   grain                胶片颗粒
 *   chromaticAberration  色差（RGB 分离）
 *   transitionDuration   参数变化时的过渡秒数
 *   interaction          repel | attract | none —— 鼠标排斥 / 吸引 / 无
 *   interactionRadius    交互影响半径（世界单位）
 *   interactionStrength  交互力度
 *   rippleIntensity      交互涟漪
 *   holdToGather         按住鼠标是否把碎片聚拢过来
 *
 * 失败降级：WebGL 不可用时自动退回静态 CSS 渐变背景，页面功能不受影响。
 */
(function (global) {
    'use strict';

    var DEFAULTS = {
        backgroundColor: '#120F17',
        shardColor: '#896ABD',
        accentColor: '#A855F7',
        placement: 'full',
        flow: 'stream',
        material: 'pearl',
        detail: 'balanced',
        effect: 'none',
        scale: 1,
        spread: 1,
        depth: 1,
        speed: 1,
        spin: 1,
        interaction: 'repel',
        density: 1.5,
        shardSize: 1.1,
        stretch: 1,
        turbulence: 1,
        glow: 1,
        edgeSoftness: 2,
        bloom: 0.5,
        grain: 0.05,
        chromaticAberration: 0.0075,
        transitionDuration: 1,
        interactionRadius: 1.5,
        interactionStrength: 0.5,
        rippleIntensity: 1,
        holdToGather: true,
        // 扩展项
        resolutionScale: 0.85,   // 内部渲染分辨率（软边图形，缩放几乎无损，省 GPU）
        maxDpr: 1.5,
        maxFps: 60
    };

    var FLOW = { stream: 0, drift: 1, orbit: 2 };
    var MATERIAL = { pearl: 0, glass: 1, metal: 2, matte: 3 };
    var DETAIL_R = { low: 1, balanced: 1, high: 2 };
    var INTERACTION = { none: 0, repel: 1, attract: -1 };

    var VERT = [
        'attribute vec2 a_pos;',
        'void main(){ gl_Position = vec4(a_pos, 0.0, 1.0); }'
    ].join('\n');

    function fragSource(radius) {
        return [
            'precision highp float;',
            '#define R ' + radius,
            'uniform vec2  u_res;',
            'uniform float u_time;',
            'uniform vec2  u_mouseWorld;',
            'uniform float u_down;',
            'uniform vec3  u_bg;',
            'uniform vec3  u_shard;',
            'uniform vec3  u_accent;',
            'uniform float u_aspect;',
            'uniform float u_scale;',
            'uniform float u_spread;',
            'uniform float u_depth;',
            'uniform float u_speed;',
            'uniform float u_spin;',
            'uniform float u_density;',
            'uniform float u_shardSize;',
            'uniform float u_stretch;',
            'uniform float u_turb;',
            'uniform float u_glow;',
            'uniform float u_edge;',
            'uniform float u_bloom;',
            'uniform float u_grain;',
            'uniform float u_ca;',
            'uniform float u_irad;',
            'uniform float u_istr;',
            'uniform float u_ripple;',
            'uniform float u_hold;',
            'uniform float u_interMode;',
            'uniform float u_flowMode;',
            'uniform float u_material;',
            'uniform float u_mask;',
            'uniform float u_vignette;',
            '',
            'float hash21(vec2 p){',
            '  p = fract(p * vec2(123.34, 456.21));',
            '  p += dot(p, p + 45.32);',
            '  return fract(p.x * p.y);',
            '}',
            'vec2 hash22(vec2 p){ return vec2(hash21(p), hash21(p + 19.19)); }',
            'mat2 rot(float a){ float c = cos(a), s = sin(a); return mat2(c, -s, s, c); }',
            'float sdRoundBox(vec2 p, vec2 b, float r){',
            '  vec2 q = abs(p) - b + r;',
            '  return min(max(q.x, q.y), 0.0) + length(max(q, 0.0)) - r;',
            '}',
            '',
            'vec2 flowShift(float t){',
            '  if (u_flowMode < 0.5) return vec2(t * 0.30 * u_speed, 0.0);',
            '  if (u_flowMode < 1.5) return vec2(t * 0.20 * u_speed, t * 0.085 * u_speed);',
            '  return vec2(sin(t * 0.16 * u_speed) * 1.6, cos(t * 0.13 * u_speed) * 1.1);',
            '}',
            '',
            '// x=SDF距离 y=辉光累计 z=深度 w=碎片内位置(0~1, 用于珍珠高光带)',
            'vec4 field(vec2 p, vec2 sh){',
            '  float cells = max(3.0, u_density * 6.0 * u_scale) * max(0.4, u_spread);',
            '  vec2 sgv = p * cells - sh;',
            '  vec2 id = floor(sgv);',
            '  vec2 f  = sgv - id;',
            '  vec4 acc = vec4(1e9, 0.0, 0.5, 0.5);',
            '  for (int j = -R; j <= R; j++){',
            '    for (int i = -R; i <= R; i++){',
            '      vec2 nid = id + vec2(float(i), float(j));',
            '      vec2 rnd = hash22(nid);',
            '      float r2 = hash21(nid * 1.37 + 4.2);',
            '      float r3 = hash21(nid * 2.11 + 9.7);',
            '      vec2 c = vec2(float(i), float(j)) + 0.5 + (rnd - 0.5) * 0.66;',
            '      vec2 q = f - c;',
            '      q += 0.045 * u_turb * vec2(sin(u_time * 0.90 + r3 * 6.283 + nid.x * 0.7),',
            '                                 cos(u_time * 1.10 + rnd.x * 6.283 + nid.y * 0.7));',
            '      float z = mix(0.30, 1.0, r3);',
            '      float par = mix(0.72, 1.30, clamp(u_depth * z, 0.0, 1.4));',
            '      vec2 qw = q / cells * par;',
            '      qw.x *= u_aspect;',
            '      vec2 wpos = (nid + c) / cells * par;',
            '      wpos.x *= u_aspect;',
            '',
            '      vec2 md = wpos - u_mouseWorld;',
            '      float mdist = length(md);',
            '      float fall = smoothstep(u_irad * 0.45, 0.0, mdist);',
            '      vec2 dir = mdist > 1e-5 ? md / mdist : vec2(0.0);',
            '      float amt = u_istr * 0.24 * fall;',
            '      qw -= dir * amt * u_interMode;',
            '      qw += dir * amt * 1.15 * u_hold * u_down;',
            '      qw += normalize(qw + 1e-5) * sin(mdist * 16.0 - u_time * 2.2) * 0.008 * u_ripple * fall;',
            '',
            '      float ang = (rnd.y - 0.5) * 3.1416 + u_time * u_spin * (r2 - 0.5) * 1.05;',
            '      qw = rot(ang) * qw;',
            '',
            '      float base = 0.052 * u_shardSize * mix(0.55, 1.30, r2) * par;',
            '      vec2 hsz = vec2(base * (1.0 + 0.85 * u_stretch * r2), base * 0.52);',
            '      float d = sdRoundBox(qw, hsz, min(hsz.x, hsz.y) * 0.40);',
            '',
            '      float g = exp(-max(d, 0.0) * (40.0 / max(0.15, u_edge))) * u_glow * mix(0.30, 1.0, z);',
            '      float shade = clamp(qw.x / max(hsz.x, 1e-4) * 0.5 + 0.5, 0.0, 1.0) * 0.7',
            '                  + clamp(qw.y / max(hsz.y, 1e-4) * 0.5 + 0.5, 0.0, 1.0) * 0.3;',
            '      if (d < acc.x) acc = vec4(d, acc.y + g, z, shade);',
            '      else acc.y += g;',
            '    }',
            '  }',
            '  return acc;',
            '}',
            '',
            'vec3 bodyColor(vec4 f, float aa){',
            '  float d = f.x, z = f.z, shade = f.w;',
            '  float alpha = 1.0 - smoothstep(-aa, aa, d);',
            '  float sheen = pow(clamp(1.0 - abs(shade * 2.0 - 1.0), 0.0, 1.0), 2.0);',
            '  vec3 col;',
            '  if (u_material < 0.5) {',
            '    col = mix(u_shard, u_accent, sheen * 0.70 + (1.0 - z) * 0.25);',
            '    col += u_accent * pow(sheen, 3.0) * 0.35;',
            '  } else if (u_material < 1.5) {',
            '    col = mix(u_shard, u_accent, 0.35) + vec3(sheen * 0.22);',
            '  } else if (u_material < 2.5) {',
            '    col = mix(u_shard * 1.15, u_accent, pow(shade, 2.0));',
            '    col += vec3(0.22) * pow(sheen, 4.0);',
            '  } else {',
            '    col = mix(u_shard, u_accent, 0.15);',
            '  }',
            '  float rim = smoothstep(0.014, 0.0, abs(d)) * 0.65;',
            '  col += u_accent * rim * 0.40;',
            '  return col * alpha;',
            '}',
            '',
            'void main(){',
            '  vec2 uv = gl_FragCoord.xy / u_res;',
            '  vec2 p = (uv - 0.5) * vec2(u_aspect, 1.0);',
            '  vec2 sh = flowShift(u_time);',
            '',
            '  vec4 c0 = field(p, sh);',
            '  vec4 cR = c0, cB = c0;',
            '  float ca = u_ca * 1.2;',
            '  if (ca > 0.00005) {',
            '    cR = field(p + vec2(ca, 0.0), sh);',
            '    cB = field(p - vec2(ca, 0.0), sh);',
            '  }',
            '  float aa = mix(0.0016, 0.011, clamp(u_edge * 0.5, 0.0, 1.0)) * (0.55 + (1.0 - c0.z));',
            '  vec3 bR = bodyColor(cR, aa);',
            '  vec3 bG = bodyColor(c0, aa);',
            '  vec3 bB = bodyColor(cB, aa);',
            '',
            '  vec3 col = u_bg + vec3(bR.r, bG.g, bB.b);',
            '  col += vec3(cR.y, c0.y, cB.y) * u_bloom * 0.55;',
            '  col += u_accent * 0.05 * smoothstep(0.95, 0.0, length(p * vec2(0.90, 1.25)));',
            '',
            '  float g = hash21(gl_FragCoord.xy * 1.7 + fract(u_time) * 311.0) - 0.5;',
            '  col += g * u_grain * 1.5;',
            '',
            '  if (u_vignette > 0.5) {',
            '    float vig = smoothstep(1.30, 0.30, length(p * vec2(1.0, 1.28)));',
            '    col *= mix(0.76, 1.0, vig);',
            '  }',
            '  col = mix(u_bg, col, u_mask);',
            '  gl_FragColor = vec4(max(col, 0.0), 1.0);',
            '}'
        ].join('\n');
    }

    function hexToRgb(hex) {
        var h = String(hex || '').trim().replace('#', '');
        if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
        var n = parseInt(h, 16);
        if (isNaN(n)) return [0, 0, 0];
        return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
    }

    function compile(gl, type, src) {
        var s = gl.createShader(type);
        gl.shaderSource(s, src);
        gl.compileShader(s);
        if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
            var log = gl.getShaderInfoLog(s);
            gl.deleteShader(s);
            throw new Error('shader compile failed: ' + log);
        }
        return s;
    }

    function Shards(canvas, options) {
        this.canvas = canvas;
        this.opts = Object.assign({}, DEFAULTS, options || {});
        this.cur = Object.assign({}, this.opts);       // 过渡用当前值
        this.mouse = { x: 0.5, y: 0.5, down: 0, inside: false };
        this.running = false;
        this.raf = 0;
        this.lastT = 0;
        this.t0 = performance.now();
        this.fpsAcc = 0;
        this.fpsCount = 0;
        this._bound = false;
        this.ok = this._initGL();
        if (this.ok) {
            this._bindEvents();
            this.start();
        } else {
            this._fallback();
        }
    }

    Shards.prototype._initGL = function () {
        var attrs = { alpha: false, antialias: false, depth: false, stencil: false,
                      premultipliedAlpha: false };
        var gl = null;
        try {
            gl = this.canvas.getContext('webgl', attrs) ||
                 this.canvas.getContext('experimental-webgl', attrs);
        } catch (e) { gl = null; }
        if (!gl) {
            this.reason = '浏览器/显卡不支持 WebGL';
            return false;
        }
        this.gl = gl;

        var radius = DETAIL_R[this.opts.detail] != null ? DETAIL_R[this.opts.detail] : 1;
        try {
            var vs = compile(gl, gl.VERTEX_SHADER, VERT);
            var fs = compile(gl, gl.FRAGMENT_SHADER, fragSource(radius));
            var prog = gl.createProgram();
            gl.attachShader(prog, vs);
            gl.attachShader(prog, fs);
            gl.bindAttribLocation(prog, 0, 'a_pos');
            gl.linkProgram(prog);
            if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
                throw new Error('link failed: ' + gl.getProgramInfoLog(prog));
            }
            gl.useProgram(prog);
            this.prog = prog;

            var buf = gl.createBuffer();
            gl.bindBuffer(gl.ARRAY_BUFFER, buf);
            gl.bufferData(gl.ARRAY_BUFFER,
                new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
            gl.enableVertexAttribArray(0);
            gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

            this.u = {};
            var names = ['u_res', 'u_time', 'u_mouseWorld', 'u_down', 'u_bg', 'u_shard', 'u_accent',
                'u_aspect', 'u_scale', 'u_spread', 'u_depth', 'u_speed', 'u_spin', 'u_density',
                'u_shardSize', 'u_stretch', 'u_turb', 'u_glow', 'u_edge', 'u_bloom', 'u_grain',
                'u_ca', 'u_irad', 'u_istr', 'u_ripple', 'u_hold', 'u_interMode', 'u_flowMode',
                'u_material', 'u_mask', 'u_vignette'];
            for (var i = 0; i < names.length; i++) {
                this.u[names[i]] = gl.getUniformLocation(prog, names[i]);
            }
            this.renderer = (function (g) {
                try {
                    var ext = g.getExtension('WEBGL_debug_renderer_info');
                    return ext ? g.getParameter(ext.UNMASKED_RENDERER_WEBGL) : 'unknown';
                } catch (e) { return 'unknown'; }
            })(gl);
            return true;
        } catch (e) {
            this.reason = e.message;
            if (global.console) console.warn('[AeroShards] WebGL 初始化失败，退回静态背景:', e.message);
            return false;
        }
    };

    Shards.prototype._fallback = function () {
        var o = this.opts;
        this.canvas.style.background =
            'radial-gradient(120% 90% at 20% 0%, ' + o.accentColor + '22 0%, transparent 55%),' +
            'radial-gradient(100% 80% at 85% 30%, ' + o.shardColor + '26 0%, transparent 60%),' +
            o.backgroundColor;
        // 便于排查：把降级原因挂在 DOM 上（不影响用户）
        this.canvas.setAttribute('data-aeroshards-fallback', this.reason || 'unknown');
    };

    Shards.prototype._bindEvents = function () {
        var self = this;
        this._onMove = function (e) {
            var r = self.canvas.getBoundingClientRect();
            var cx = (e.touches ? e.touches[0].clientX : e.clientX) - r.left;
            var cy = (e.touches ? e.touches[0].clientY : e.clientY) - r.top;
            self.mouse.x = cx / Math.max(1, r.width);
            self.mouse.y = 1 - cy / Math.max(1, r.height);   // WebGL 原点在左下
            self.mouse.inside = true;
        };
        this._onDown = function (e) {
            self.mouse.down = 1;
            self._onMove(e);
        };
        this._onUp = function () { self.mouse.down = 0; };
        this._onLeave = function () { self.mouse.inside = false; self.mouse.down = 0; };

        global.addEventListener('pointermove', this._onMove, { passive: true });
        global.addEventListener('pointerdown', this._onDown, { passive: true });
        global.addEventListener('pointerup', this._onUp, { passive: true });
        global.addEventListener('pointercancel', this._onUp, { passive: true });
        global.addEventListener('blur', this._onUp);
        document.addEventListener('visibilitychange', function () {
            if (document.hidden) self.pause(); else self.start();
        });
        this._onResize = function () { self.resize(); };
        global.addEventListener('resize', this._onResize);
        this._bound = true;
    };

    Shards.prototype.resize = function () {
        if (!this.ok) return;
        var o = this.opts;
        var dpr = Math.min(global.devicePixelRatio || 1, o.maxDpr) * o.resolutionScale;
        var w = Math.max(1, Math.floor(this.canvas.clientWidth * dpr));
        var h = Math.max(1, Math.floor(this.canvas.clientHeight * dpr));
        if (this.canvas.width !== w || this.canvas.height !== h) {
            this.canvas.width = w;
            this.canvas.height = h;
            this.gl.viewport(0, 0, w, h);
        }
    };

    Shards.prototype.setOptions = function (patch) {
        Object.assign(this.opts, patch || {});
    };

    Shards.prototype._lerp = function (dt) {
        var d = Math.max(0, this.opts.transitionDuration || 0);
        var k = d <= 0 ? 1 : Math.min(1, dt / d);
        var keys = Object.keys(DEFAULTS);
        for (var i = 0; i < keys.length; i++) {
            var key = keys[i];
            var a = this.cur[key], b = this.opts[key];
            if (typeof b === 'number' && typeof a === 'number') {
                this.cur[key] = a + (b - a) * k;
            } else {
                this.cur[key] = b;
            }
        }
    };

    Shards.prototype._draw = function (now) {
        var gl = this.gl, u = this.u, o = this.cur;
        var dt = Math.min(0.1, (now - this.lastT) / 1000 || 0.016);
        this.lastT = now;
        this._lerp(dt);
        this.resize();

        var t = (now - this.t0) / 1000;
        var w = this.canvas.width, h = this.canvas.height;
        var aspect = w / Math.max(1, h);

        var mw = [(this.mouse.x - 0.5) * aspect, this.mouse.y - 0.5];

        gl.uniform2f(u.u_res, w, h);
        gl.uniform1f(u.u_time, t);
        gl.uniform2f(u.u_mouseWorld, mw[0], mw[1]);
        gl.uniform1f(u.u_down, this.mouse.down);
        var bg = hexToRgb(o.backgroundColor);
        var sc = hexToRgb(o.shardColor);
        var ac = hexToRgb(o.accentColor);
        gl.uniform3f(u.u_bg, bg[0], bg[1], bg[2]);
        gl.uniform3f(u.u_shard, sc[0], sc[1], sc[2]);
        gl.uniform3f(u.u_accent, ac[0], ac[1], ac[2]);
        gl.uniform1f(u.u_aspect, aspect);
        gl.uniform1f(u.u_scale, o.scale);
        gl.uniform1f(u.u_spread, o.spread);
        gl.uniform1f(u.u_depth, o.depth);
        gl.uniform1f(u.u_speed, o.speed);
        gl.uniform1f(u.u_spin, o.spin);
        gl.uniform1f(u.u_density, o.density);
        gl.uniform1f(u.u_shardSize, o.shardSize);
        gl.uniform1f(u.u_stretch, o.stretch);
        gl.uniform1f(u.u_turb, o.turbulence);
        gl.uniform1f(u.u_glow, o.glow);
        gl.uniform1f(u.u_edge, o.edgeSoftness);
        gl.uniform1f(u.u_bloom, o.bloom);
        gl.uniform1f(u.u_grain, o.grain);
        gl.uniform1f(u.u_ca, o.chromaticAberration);
        gl.uniform1f(u.u_irad, o.interactionRadius);
        gl.uniform1f(u.u_istr, o.interactionStrength);
        gl.uniform1f(u.u_ripple, o.rippleIntensity);
        gl.uniform1f(u.u_hold, o.holdToGather ? 1 : 0);
        gl.uniform1f(u.u_interMode, INTERACTION[o.interaction] != null ? INTERACTION[o.interaction] : 1);
        gl.uniform1f(u.u_flowMode, FLOW[o.flow] != null ? FLOW[o.flow] : 0);
        gl.uniform1f(u.u_material, MATERIAL[o.material] != null ? MATERIAL[o.material] : 0);
        var mask = 1.0;
        if (o.placement === 'top') mask = 1.0;
        else if (o.placement === 'center') mask = 1.0;
        gl.uniform1f(u.u_mask, mask);
        gl.uniform1f(u.u_vignette, o.effect === 'vignette' ? 1 : 0);

        gl.drawArrays(gl.TRIANGLES, 0, 3);
    };

    Shards.prototype._loop = function (now) {
        if (!this.running) return;
        var self = this;
        this.raf = global.requestAnimationFrame(function (n) { self._loop(n); });
        var minDelta = 1000 / Math.max(1, this.opts.maxFps || 60);
        if (now - this.lastT < minDelta - 1) return;
        try {
            this._draw(now);
        } catch (e) {
            if (global.console) console.warn('[AeroShards] 渲染异常，已停止:', e.message);
            this.stop();
        }
    };

    Shards.prototype.start = function () {
        if (!this.ok || this.running) return;
        this.running = true;
        this.lastT = performance.now();
        var self = this;
        this.raf = global.requestAnimationFrame(function (n) { self._loop(n); });
    };

    Shards.prototype.pause = function () { this.running = false; };

    Shards.prototype.stop = function () {
        this.running = false;
        if (this.raf) global.cancelAnimationFrame(this.raf);
        this.raf = 0;
    };

    Shards.prototype.destroy = function () {
        this.stop();
        if (this._bound) {
            global.removeEventListener('pointermove', this._onMove);
            global.removeEventListener('pointerdown', this._onDown);
            global.removeEventListener('pointerup', this._onUp);
            global.removeEventListener('pointercancel', this._onUp);
            global.removeEventListener('blur', this._onUp);
            global.removeEventListener('resize', this._onResize);
            this._bound = false;
        }
    };

    var AeroShards = {
        DEFAULTS: DEFAULTS,
        create: function (canvasOrId, options) {
            var el = typeof canvasOrId === 'string'
                ? document.getElementById(canvasOrId) : canvasOrId;
            if (!el) {
                if (global.console) console.warn('[AeroShards] 找不到画布元素');
                return null;
            }
            return new Shards(el, options);
        },
        /** 从 canvas 的 data-* 属性读取配置并初始化（data- 值会自动转数字/布尔） */
        auto: function (id) {
            var el = document.getElementById(id);
            if (!el) return null;
            var opts = {};
            var d = el.dataset || {};
            Object.keys(DEFAULTS).forEach(function (k) {
                var attr = k.replace(/[A-Z]/g, function (m) { return '-' + m.toLowerCase(); });
                var v = d[k] != null ? d[k] : d[attr];
                if (v == null) return;
                if (v === 'true') opts[k] = true;
                else if (v === 'false') opts[k] = false;
                else if (v !== '' && !isNaN(Number(v))) opts[k] = Number(v);
                else opts[k] = v;
            });
            return new Shards(el, opts);
        }
    };

    global.AeroShards = AeroShards;
    if (typeof module !== 'undefined' && module.exports) module.exports = AeroShards;
})(typeof window !== 'undefined' ? window : this);
