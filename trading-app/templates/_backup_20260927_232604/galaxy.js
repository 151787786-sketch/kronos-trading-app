/*!
 * Galaxy — ReactBits <Galaxy /> 的零依赖 vanilla 移植
 *
 * 原组件依赖 `ogl`（Renderer/Program/Mesh/Triangle）。这里去掉 ogl，直接用原生 WebGL
 * 复刻同样的全屏三角形 + 同一份 fragment shader，渲染结果一致，但不引入任何依赖。
 *
 * 与原组件一一对应的 props（默认值与原版完全相同）：
 *   focal [0.5,0.5]     旋转中心（0~1 归一化）
 *   rotation [1,0]      额外旋转
 *   starSpeed 0.5       星星流动速度
 *   density 1           密度（越大星星越密）
 *   hueShift 140        色相偏移（度）
 *   speed 1.0           整体速度
 *   mouseInteraction    true  鼠标交互开关
 *   glowIntensity 0.3   辉光强度
 *   saturation 0.0      饱和度（0 = 保持原色）
 *   mouseRepulsion true 鼠标排斥
 *   repulsionStrength 2 排斥力度
 *   twinkleIntensity 0.3 闪烁强度
 *   rotationSpeed 0.1   自转速度
 *   autoCenterRepulsion 0 中心自动排斥
 *   transparent true    背景透明（叠加到页面底色上）
 *   lightMode false     浅色模式
 *
 * 原版 shader 里的 float 循环计数器在严格 GLSL ES 1.0 下不合法（部分驱动会编译失败），
 * 这里改成常量边界的 int 循环，其余 shader 代码逐行保持原样。
 */
(function (global) {
    'use strict';

    var VERT = [
        'attribute vec2 uv;',
        'attribute vec2 position;',
        'varying vec2 vUv;',
        'void main() {',
        '  vUv = uv;',
        '  gl_Position = vec4(position, 0, 1);',
        '}'
    ].join('\n');

    var FRAG = [
        'precision highp float;',
        '',
        'uniform float uTime;',
        'uniform vec3  uResolution;',
        'uniform vec2  uFocal;',
        'uniform vec2  uRotation;',
        'uniform float uStarSpeed;',
        'uniform float uDensity;',
        'uniform float uHueShift;',
        'uniform float uSpeed;',
        'uniform vec2  uMouse;',
        'uniform float uGlowIntensity;',
        'uniform float uSaturation;',
        'uniform float uMouseRepulsion;',
        'uniform float uTwinkleIntensity;',
        'uniform float uRotationSpeed;',
        'uniform float uRepulsionStrength;',
        'uniform float uMouseActiveFactor;',
        'uniform float uAutoCenterRepulsion;',
        'uniform float uTransparent;',
        'uniform float uLightMode;',
        '',
        'varying vec2 vUv;',
        '',
        '#define NUM_LAYER 4.0',
        '#define STAR_COLOR_CUTOFF 0.2',
        '#define MAT45 mat2(0.7071, -0.7071, 0.7071, 0.7071)',
        '#define PERIOD 3.0',
        '',
        'float Hash21(vec2 p) {',
        '  p = fract(p * vec2(123.34, 456.21));',
        '  p += dot(p, p + 45.32);',
        '  return fract(p.x * p.y);',
        '}',
        '',
        'float tri(float x) { return abs(fract(x) * 2.0 - 1.0); }',
        '',
        'float tris(float x) {',
        '  float t = fract(x);',
        '  return 1.0 - smoothstep(0.0, 1.0, abs(2.0 * t - 1.0));',
        '}',
        '',
        'float trisn(float x) {',
        '  float t = fract(x);',
        '  return 2.0 * (1.0 - smoothstep(0.0, 1.0, abs(2.0 * t - 1.0))) - 1.0;',
        '}',
        '',
        'vec3 hsv2rgb(vec3 c) {',
        '  vec4 K = vec4(1.0, 2.0 / 3.0, 1.0 / 3.0, 3.0);',
        '  vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);',
        '  return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);',
        '}',
        '',
        'float Star(vec2 uv, float flare) {',
        '  float d = length(uv);',
        '  float m = (0.05 * uGlowIntensity) / d;',
        '  float rays = smoothstep(0.0, 1.0, 1.0 - abs(uv.x * uv.y * 1000.0));',
        '  m += rays * flare * uGlowIntensity;',
        '  uv *= MAT45;',
        '  rays = smoothstep(0.0, 1.0, 1.0 - abs(uv.x * uv.y * 1000.0));',
        '  m += rays * 0.3 * flare * uGlowIntensity;',
        '  m *= smoothstep(1.0, 0.2, d);',
        '  return m;',
        '}',
        '',
        'vec3 StarLayer(vec2 uv) {',
        '  vec3 col = vec3(0.0);',
        '  vec2 gv = fract(uv) - 0.5;',
        '  vec2 id = floor(uv);',
        '',
        '  for (int y = -1; y <= 1; y++) {',
        '    for (int x = -1; x <= 1; x++) {',
        '      vec2 offset = vec2(float(x), float(y));',
        '      vec2 si = id + vec2(float(x), float(y));',
        '      float seed = Hash21(si);',
        '      float size = fract(seed * 345.32);',
        '      float glossLocal = tri(uStarSpeed / (PERIOD * seed + 1.0));',
        '      float flareSize = smoothstep(0.9, 1.0, size) * glossLocal;',
        '',
        '      float red = smoothstep(STAR_COLOR_CUTOFF, 1.0, Hash21(si + 1.0)) + STAR_COLOR_CUTOFF;',
        '      float blu = smoothstep(STAR_COLOR_CUTOFF, 1.0, Hash21(si + 3.0)) + STAR_COLOR_CUTOFF;',
        '      float grn = min(red, blu) * seed;',
        '      vec3 base = vec3(red, grn, blu);',
        '',
        '      float hue = atan(base.g - base.r, base.b - base.r) / (2.0 * 3.14159) + 0.5;',
        '      hue = fract(hue + uHueShift / 360.0);',
        '      float sat = length(base - vec3(dot(base, vec3(0.299, 0.587, 0.114)))) * uSaturation;',
        '      float val = max(max(base.r, base.g), base.b);',
        '      base = hsv2rgb(vec3(hue, sat, val));',
        '',
        '      vec2 pad = vec2(tris(seed * 34.0 + uTime * uSpeed / 10.0),',
        '                      tris(seed * 38.0 + uTime * uSpeed / 30.0)) - 0.5;',
        '',
        '      float star = Star(gv - offset - pad, flareSize);',
        '      vec3 color = base;',
        '',
        '      float twinkle = trisn(uTime * uSpeed + seed * 6.2831) * 0.5 + 1.0;',
        '      twinkle = mix(1.0, twinkle, uTwinkleIntensity);',
        '      star *= twinkle;',
        '',
        '      col += star * size * color;',
        '    }',
        '  }',
        '  return col;',
        '}',
        '',
        'void main() {',
        '  vec2 focalPx = uFocal * uResolution.xy;',
        '  vec2 uv = (vUv * uResolution.xy - focalPx) / uResolution.y;',
        '',
        '  vec2 mouseNorm = uMouse - vec2(0.5);',
        '',
        '  if (uAutoCenterRepulsion > 0.0) {',
        '    vec2 centerUV = vec2(0.0, 0.0);',
        '    float centerDist = length(uv - centerUV);',
        '    vec2 repulsion = normalize(uv - centerUV) * (uAutoCenterRepulsion / (centerDist + 0.1));',
        '    uv += repulsion * 0.05;',
        '  } else if (uMouseRepulsion > 0.5) {',
        '    vec2 mousePosUV = (uMouse * uResolution.xy - focalPx) / uResolution.y;',
        '    float mouseDist = length(uv - mousePosUV);',
        '    vec2 repulsion = normalize(uv - mousePosUV) * (uRepulsionStrength / (mouseDist + 0.1));',
        '    uv += repulsion * 0.05 * uMouseActiveFactor;',
        '  } else {',
        '    vec2 mouseOffset = mouseNorm * 0.1 * uMouseActiveFactor;',
        '    uv += mouseOffset;',
        '  }',
        '',
        '  float autoRotAngle = uTime * uRotationSpeed;',
        '  mat2 autoRot = mat2(cos(autoRotAngle), -sin(autoRotAngle), sin(autoRotAngle), cos(autoRotAngle));',
        '  uv = autoRot * uv;',
        '  uv = mat2(uRotation.x, -uRotation.y, uRotation.y, uRotation.x) * uv;',
        '',
        '  vec3 col = vec3(0.0);',
        '  // 原版用 float 循环计数器；GLSL ES 1.0 严格模式不允许，这里改成常量边界 int 循环',
        '  for (int li = 0; li < 4; li++) {',
        '    float i = float(li) / NUM_LAYER;',
        '    float depth = fract(i + uStarSpeed * uSpeed);',
        '    float scale = mix(20.0 * uDensity, 0.5 * uDensity, depth);',
        '    float fade = depth * smoothstep(1.0, 0.9, depth);',
        '    col += StarLayer(uv * scale + i * 453.32) * fade;',
        '  }',
        '',
        '  if (uLightMode > 0.5) {',
        '    float energy = max(max(col.r, col.g), col.b);',
        '    float coverage = clamp(smoothstep(0.0, 0.42, energy) * 0.92, 0.0, 0.92);',
        '    vec3 ink = clamp(col * 0.48, 0.0, 0.82);',
        '    gl_FragColor = vec4(mix(vec3(1.0), ink, coverage), 1.0);',
        '  } else if (uTransparent > 0.5) {',
        '    float alpha = length(col);',
        '    alpha = smoothstep(0.0, 0.3, alpha);',
        '    alpha = min(alpha, 1.0);',
        '    gl_FragColor = vec4(col, alpha);',
        '  } else {',
        '    gl_FragColor = vec4(col, 1.0);',
        '  }',
        '}'
    ].join('\n');

    var DEFAULTS = {
        focal: [0.5, 0.5],
        rotation: [1.0, 0.0],
        starSpeed: 0.5,
        density: 1,
        hueShift: 140,
        disableAnimation: false,
        speed: 1.0,
        mouseInteraction: true,
        glowIntensity: 0.3,
        saturation: 0.0,
        mouseRepulsion: true,
        repulsionStrength: 2,
        twinkleIntensity: 0.3,
        rotationSpeed: 0.1,
        autoCenterRepulsion: 0,
        transparent: true,
        lightMode: false,
        // 扩展项（不影响原版行为）
        maxDpr: 1.5,
        resolutionScale: 0.85
    };

    function compile(gl, type, src) {
        var s = gl.createShader(type);
        gl.shaderSource(s, src);
        gl.compileShader(s);
        if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
            var log = gl.getShaderInfoLog(s);
            gl.deleteShader(s);
            throw new Error(log);
        }
        return s;
    }

    function Galaxy(canvas, options) {
        this.canvas = canvas;
        this.opts = Object.assign({}, DEFAULTS, options || {});
        this.mouse = { x: 0.5, y: 0.5 };
        this.smoothMouse = { x: 0.5, y: 0.5 };
        this.mouseActive = 0;
        this.smoothMouseActive = 0;
        this.running = false;
        this.raf = 0;
        this.ok = this._init();
        if (this.ok) {
            this._bind();
            this.resize();
            this.start();
        } else {
            this._fallback();
        }
    }

    Galaxy.prototype._init = function () {
        var o = this.opts;
        var attrs = { alpha: !!o.transparent, premultipliedAlpha: false, antialias: false, depth: false };
        var gl = null;
        try {
            gl = this.canvas.getContext('webgl', attrs) ||
                 this.canvas.getContext('experimental-webgl', attrs);
        } catch (e) { gl = null; }
        if (!gl) { this.reason = '浏览器/显卡不支持 WebGL'; return false; }
        this.gl = gl;

        if (o.lightMode) {
            gl.clearColor(1, 1, 1, 1);
        } else if (o.transparent) {
            gl.enable(gl.BLEND);
            gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
            gl.clearColor(0, 0, 0, 0);
        } else {
            gl.clearColor(0, 0, 0, 1);
        }

        try {
            var vs = compile(gl, gl.VERTEX_SHADER, VERT);
            var fs = compile(gl, gl.FRAGMENT_SHADER, FRAG);
            var prog = gl.createProgram();
            gl.attachShader(prog, vs);
            gl.attachShader(prog, fs);
            gl.linkProgram(prog);
            if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
                throw new Error('link failed: ' + gl.getProgramInfoLog(prog));
            }
            gl.useProgram(prog);
            this.prog = prog;

            // ogl 的 Triangle：覆盖全屏的单个三角形
            this.buf = gl.createBuffer();
            gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
            gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
            var aPos = gl.getAttribLocation(prog, 'position');
            gl.enableVertexAttribArray(aPos);
            gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

            this.uvBuf = gl.createBuffer();
            gl.bindBuffer(gl.ARRAY_BUFFER, this.uvBuf);
            gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, 0, 2, 0, 0, 2]), gl.STATIC_DRAW);
            var aUv = gl.getAttribLocation(prog, 'uv');
            gl.enableVertexAttribArray(aUv);
            gl.vertexAttribPointer(aUv, 2, gl.FLOAT, false, 0, 0);

            var names = ['uTime', 'uResolution', 'uFocal', 'uRotation', 'uStarSpeed', 'uDensity',
                'uHueShift', 'uSpeed', 'uMouse', 'uGlowIntensity', 'uSaturation', 'uMouseRepulsion',
                'uTwinkleIntensity', 'uRotationSpeed', 'uRepulsionStrength', 'uMouseActiveFactor',
                'uAutoCenterRepulsion', 'uTransparent', 'uLightMode'];
            this.u = {};
            for (var i = 0; i < names.length; i++) this.u[names[i]] = gl.getUniformLocation(prog, names[i]);

            this.applyStaticUniforms();
            return true;
        } catch (e) {
            this.reason = e.message;
            if (global.console) console.warn('[Galaxy] WebGL 初始化失败:', e.message);
            return false;
        }
    };

    Galaxy.prototype.applyStaticUniforms = function () {
        var gl = this.gl, u = this.u, o = this.opts;
        gl.uniform2f(u.uFocal, o.focal[0], o.focal[1]);
        gl.uniform2f(u.uRotation, o.rotation[0], o.rotation[1]);
        gl.uniform1f(u.uDensity, o.density);
        gl.uniform1f(u.uHueShift, o.hueShift);
        gl.uniform1f(u.uSpeed, o.speed);
        gl.uniform1f(u.uGlowIntensity, o.glowIntensity);
        gl.uniform1f(u.uSaturation, o.saturation);
        gl.uniform1f(u.uMouseRepulsion, o.mouseRepulsion ? 1 : 0);
        gl.uniform1f(u.uTwinkleIntensity, o.twinkleIntensity);
        gl.uniform1f(u.uRotationSpeed, o.rotationSpeed);
        gl.uniform1f(u.uRepulsionStrength, o.repulsionStrength);
        gl.uniform1f(u.uAutoCenterRepulsion, o.autoCenterRepulsion);
        gl.uniform1f(u.uTransparent, o.transparent ? 1 : 0);
        gl.uniform1f(u.uLightMode, o.lightMode ? 1 : 0);
        gl.uniform1f(u.uStarSpeed, o.starSpeed);
    };

    Galaxy.prototype.setOptions = function (patch) {
        Object.assign(this.opts, patch || {});
        if (this.ok) this.applyStaticUniforms();
    };

    Galaxy.prototype._fallback = function () {
        this.canvas.style.background =
            'radial-gradient(ellipse at 50% 50%, rgba(120,150,255,.28) 0%, transparent 55%),' +
            'radial-gradient(ellipse at 20% 80%, rgba(90,60,220,.18) 0%, transparent 60%), #000';
        this.canvas.setAttribute('data-galaxy-fallback', this.reason || 'unknown');
    };

    Galaxy.prototype._bind = function () {
        var self = this;
        this._onResize = function () { self.resize(); };
        global.addEventListener('resize', this._onResize);

        if (this.opts.mouseInteraction) {
            this._onMove = function (e) {
                var rect = self.canvas.getBoundingClientRect();
                if (!rect.width || !rect.height) return;
                self.mouse.x = (e.clientX - rect.left) / rect.width;
                self.mouse.y = 1.0 - (e.clientY - rect.top) / rect.height;
                self.mouseActive = 1.0;
            };
            this._onLeave = function () { self.mouseActive = 0.0; };
            global.addEventListener('mousemove', this._onMove, { passive: true });
            global.addEventListener('mouseleave', this._onLeave);
            global.addEventListener('blur', this._onLeave);
        }
        document.addEventListener('visibilitychange', function () {
            if (document.hidden) self.pause(); else self.start();
        });
    };

    Galaxy.prototype.resize = function () {
        if (!this.ok) return;
        var dpr = Math.min(global.devicePixelRatio || 1, this.opts.maxDpr) * this.opts.resolutionScale;
        var w = Math.max(1, Math.floor((this.canvas.clientWidth || global.innerWidth) * dpr));
        var h = Math.max(1, Math.floor((this.canvas.clientHeight || global.innerHeight) * dpr));
        if (this.canvas.width !== w || this.canvas.height !== h) {
            this.canvas.width = w;
            this.canvas.height = h;
            this.gl.viewport(0, 0, w, h);
        }
    };

    Galaxy.prototype._draw = function (t) {
        var gl = this.gl, u = this.u, o = this.opts;
        this.resize();

        var time = t * 0.001;
        gl.uniform1f(u.uTime, time);
        // 原版：uStarSpeed = t*0.001*starSpeed/10（会随时间增长）
        gl.uniform1f(u.uStarSpeed, o.disableAnimation ? o.starSpeed : (time * o.starSpeed) / 10.0);
        gl.uniform3f(u.uResolution, gl.canvas.width, gl.canvas.height,
                     gl.canvas.width / Math.max(1, gl.canvas.height));

        var lerp = 0.05;
        this.smoothMouse.x += (this.mouse.x - this.smoothMouse.x) * lerp;
        this.smoothMouse.y += (this.mouse.y - this.smoothMouse.y) * lerp;
        this.smoothMouseActive += (this.mouseActive - this.smoothMouseActive) * lerp;

        gl.uniform2f(u.uMouse, this.smoothMouse.x, this.smoothMouse.y);
        gl.uniform1f(u.uMouseActiveFactor, this.smoothMouseActive);

        gl.clear(gl.COLOR_BUFFER_BIT);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
    };

    Galaxy.prototype._loop = function (t) {
        if (!this.running) return;
        var self = this;
        this.raf = global.requestAnimationFrame(function (n) { self._loop(n); });
        try {
            this._draw(t);
        } catch (e) {
            if (global.console) console.warn('[Galaxy] 渲染异常，已停止:', e.message);
            this.stop();
        }
    };

    Galaxy.prototype.start = function () {
        if (!this.ok || this.running) return;
        this.running = true;
        var self = this;
        this.raf = global.requestAnimationFrame(function (n) { self._loop(n); });
    };

    Galaxy.prototype.pause = function () { this.running = false; };

    Galaxy.prototype.stop = function () {
        this.running = false;
        if (this.raf) global.cancelAnimationFrame(this.raf);
        this.raf = 0;
    };

    Galaxy.prototype.destroy = function () {
        this.stop();
        global.removeEventListener('resize', this._onResize);
        if (this._onMove) {
            global.removeEventListener('mousemove', this._onMove);
            global.removeEventListener('mouseleave', this._onLeave);
            global.removeEventListener('blur', this._onLeave);
        }
        try {
            var ext = this.gl && this.gl.getExtension('WEBGL_lose_context');
            if (ext) ext.loseContext();
        } catch (e) {}
    };

    global.GalaxyBackground = {
        DEFAULTS: DEFAULTS,
        create: function (canvasOrId, options) {
            var el = typeof canvasOrId === 'string' ? document.getElementById(canvasOrId) : canvasOrId;
            if (!el) {
                if (global.console) console.warn('[Galaxy] 找不到画布元素');
                return null;
            }
            return new Galaxy(el, options);
        }
    };
})(typeof window !== 'undefined' ? window : this);
