/*!
 * GatewayFlow — 零依赖 vanilla 移植版
 *
 * 原组件是 React + iframe(srcDoc) + Tailwind + GSAP + iconify 的 "Nexus Gateway" 效果，
 * 核心其实是一段 canvas 2D 动画。这里把那段核心原样搬出来做成模块，
 * 不引入 React / Tailwind / GSAP / 任何 CDN，参数与语义保持一致：
 *
 *   GatewayFlow.create(canvasEl, {
 *     speed: 1,          // 粒子流动速度倍率
 *     size: 1,           // 线宽倍率
 *     gap: 2,            // 虚线间隔倍率
 *     length: 1,         // 虚线长度倍率
 *     density: 1,        // 流线数量倍率（原版 80 条）
 *     strokeWidth: 1,    // 线宽额外倍率
 *     opacity: 1,        // 画布透明度
 *     hue: 0, saturation: 1, brightness: 1,   // 对应原版的 CSS filter
 *     mode: 'dark',      // dark | light
 *     interactive: true  // 点击产生爆破涟漪（原版行为）
 *   });
 *
 * 视觉：左右两侧各一组贝塞尔曲线汇聚到画面中心，白色虚线描边，
 *       每根线上有一颗 3×3 白色粒子沿曲线流动；点击时产生环形冲击把粒子推开。
 */
(function (global) {
    'use strict';

    var BASE_PATHS = 80;
    var BASE_LINE_WIDTH = 1.2;
    var BASE_DASH = [1, 4];
    var PARTICLE_SIZE = 3;

    var DARK = {
        stroke: 'rgba(255, 255, 255, 0.35)',
        particle: 'rgba(255, 255, 255, 0.70)'
    };
    var LIGHT = {
        stroke: 'rgba(26, 31, 42, 0.40)',
        particle: 'rgba(26, 31, 42, 0.75)'
    };

    var DEFAULTS = {
        speed: 1,
        size: 1,
        gap: 2,
        length: 1,
        density: 1,
        strokeWidth: 1,
        opacity: 1,
        hue: 0,
        saturation: 1,
        brightness: 1,
        mode: 'dark',
        interactive: true,
        maxDpr: 1.5
    };

    function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }

    function bezierPoint(t, p0, p1, p2, p3) {
        var u = 1 - t, u2 = u * u, t2 = t * t;
        return {
            x: u2 * u * p0.x + 3 * u2 * t * p1.x + 3 * u * t2 * p2.x + t2 * t * p3.x,
            y: u2 * u * p0.y + 3 * u2 * t * p1.y + 3 * u * t2 * p2.y + t2 * t * p3.y
        };
    }

    function Flow(canvas, options) {
        this.canvas = canvas;
        this.ctx = canvas.getContext('2d');
        this.opts = Object.assign({}, DEFAULTS, options || {});
        this.cur = Object.assign({}, this.opts);
        this.paths = [];
        this.explosions = [];
        this.width = 0;
        this.height = 0;
        this.running = false;
        this.raf = 0;
        this.fpsAcc = 0;
        this.fpsFrames = 0;
        this.onFps = null;

        this._build();
        this.resize();
        this._bind();
        this.start();
    }

    Flow.prototype._palette = function () {
        return this.opts.mode === 'light' ? LIGHT : DARK;
    };

    Flow.prototype._build = function () {
        var n = Math.max(12, Math.round(BASE_PATHS * clamp(this.opts.density, 0.25, 2.5)));
        var h = this.height || (global.innerHeight || 800);
        var paths = [];
        for (var i = 0; i < n; i++) {
            paths.push({
                isLeft: i % 2 === 0,
                // 原版：(i / n) * height * 1.4 - height * 0.2 —— 让起点铺满并略微超出上下边界
                startY: (i / n) * h * 1.4 - h * 0.2,
                particles: [{
                    t: Math.random(),
                    speed: 0.0015 + Math.random() * 0.002
                }]
            });
        }
        this.paths = paths;
    };

    Flow.prototype.resize = function () {
        var dpr = Math.min(global.devicePixelRatio || 1, this.opts.maxDpr);
        var w = this.canvas.clientWidth || global.innerWidth || 800;
        var h = this.canvas.clientHeight || global.innerHeight || 600;
        var changed = (w !== this.width || h !== this.height);
        this.width = w;
        this.height = h;
        this.canvas.width = Math.max(1, Math.floor(w * dpr));
        this.canvas.height = Math.max(1, Math.floor(h * dpr));
        this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        // 尺寸变化时重算起点，避免流线跑出画面
        if (changed) {
            var n = this.paths.length;
            for (var i = 0; i < n; i++) {
                this.paths[i].startY = (i / n) * h * 1.4 - h * 0.2;
            }
        }
    };

    Flow.prototype._bind = function () {
        var self = this;
        this._onResize = function () { self.resize(); };
        global.addEventListener('resize', this._onResize);
        if (this.opts.interactive) {
            this._onClick = function (e) {
                self.explosions.push({ x: e.clientX, y: e.clientY, radius: 0, life: 1 });
                if (self.explosions.length > 12) self.explosions.shift();
            };
            global.addEventListener('click', this._onClick, { passive: true });
        }
        document.addEventListener('visibilitychange', function () {
            if (document.hidden) self.pause(); else self.start();
        });
    };

    Flow.prototype._lerp = function (dt) {
        var k = Math.min(1, dt * 3);
        var keys = Object.keys(DEFAULTS);
        for (var i = 0; i < keys.length; i++) {
            var key = keys[i];
            var a = this.cur[key], b = this.opts[key];
            if (typeof b === 'number' && typeof a === 'number') this.cur[key] = a + (b - a) * k;
            else this.cur[key] = b;
        }
    };

    Flow.prototype._draw = function (dt) {
        var ctx = this.ctx, w = this.width, h = this.height;
        this._lerp(dt);
        var o = this.cur;
        var pal = this.opts.mode === 'light' ? LIGHT : DARK;
        var centerX = w / 2, centerY = h / 2;
        var lw = BASE_LINE_WIDTH * clamp(o.size, 0.05, 6) * clamp(o.strokeWidth, 0.25, 8);
        var dash = [BASE_DASH[0] * clamp(o.length, 0.35, 2.5),
                    BASE_DASH[1] * clamp(o.gap, 0.1, 8)];
        var speedMul = clamp(o.speed, 0, 3);

        ctx.clearRect(0, 0, w, h);

        // 爆破涟漪
        for (var e = this.explosions.length - 1; e >= 0; e--) {
            var exp = this.explosions[e];
            exp.radius += 15;
            exp.life -= 0.015;
            if (exp.life <= 0) this.explosions.splice(e, 1);
        }

        ctx.lineWidth = lw;
        ctx.setLineDash(dash);
        ctx.strokeStyle = pal.stroke;

        for (var i = 0; i < this.paths.length; i++) {
            var path = this.paths[i];
            var p0 = { x: path.isLeft ? 0 : w, y: path.startY };
            var p1 = { x: path.isLeft ? centerX * 0.5 : w - centerX * 0.5, y: path.startY };
            var p2 = { x: path.isLeft ? centerX * 0.8 : w - centerX * 0.8, y: centerY };
            var p3 = { x: centerX, y: centerY };

            ctx.beginPath();
            ctx.moveTo(p0.x, p0.y);
            ctx.bezierCurveTo(p1.x, p1.y, p2.x, p2.y, p3.x, p3.y);
            ctx.stroke();

            for (var k = 0; k < path.particles.length; k++) {
                var p = path.particles[k];
                p.t += p.speed * speedMul;
                if (p.t > 1) {
                    p.t = 0;
                    path.startY += (Math.random() - 0.5) * 10;
                }
                var pos = bezierPoint(p.t, p0, p1, p2, p3);
                var dxTotal = 0, dyTotal = 0;
                for (var x = 0; x < this.explosions.length; x++) {
                    var ex = this.explosions[x];
                    var dx = pos.x - ex.x, dy = pos.y - ex.y;
                    var dist = Math.sqrt(dx * dx + dy * dy) || 0.0001;
                    if (dist < ex.radius + 120 && dist > ex.radius - 120) {
                        var force = (1 - Math.abs(dist - ex.radius) / 120) * ex.life;
                        dxTotal += (dx / dist) * force * 80;
                        dyTotal += (dy / dist) * force * 80;
                    }
                }
                ctx.fillStyle = pal.particle;
                ctx.fillRect(pos.x + dxTotal - PARTICLE_SIZE / 2,
                             pos.y + dyTotal - PARTICLE_SIZE / 2,
                             PARTICLE_SIZE, PARTICLE_SIZE);
            }
        }
        ctx.setLineDash([]);
    };

    Flow.prototype._loop = function (now) {
        if (!this.running) return;
        var self = this;
        this.raf = global.requestAnimationFrame(function (n) { self._loop(n); });
        var dt = this._last ? Math.min(0.1, (now - this._last) / 1000) : 0.016;
        this._last = now;
        try {
            this._draw(dt);
        } catch (err) {
            if (global.console) console.warn('[GatewayFlow] 渲染异常，已停止:', err.message);
            this.stop();
        }
    };

    Flow.prototype.setOptions = function (patch) {
        Object.assign(this.opts, patch || {});
        if (patch && patch.density !== undefined) this._build();
        this.applyFilter();
    };

    /** 对应原版 iframe 外层的 CSS filter：hue-rotate / saturate / brightness */
    Flow.prototype.applyFilter = function () {
        var o = this.opts;
        var op = clamp(o.opacity, 0.05, 1);
        var f = [];
        if (o.hue) f.push('hue-rotate(' + clamp(o.hue, -180, 180) + 'deg)');
        if (o.saturation !== 1) f.push('saturate(' + clamp(o.saturation, 0, 2) + ')');
        if (o.brightness !== 1) f.push('brightness(' + clamp(o.brightness, 0.35, 1.65) + ')');
        this.canvas.style.opacity = String(op);
        this.canvas.style.filter = f.length ? f.join(' ') : 'none';
    };

    Flow.prototype.start = function () {
        if (this.running) return;
        this.running = true;
        this._last = 0;
        var self = this;
        this.raf = global.requestAnimationFrame(function (n) { self._loop(n); });
    };

    Flow.prototype.pause = function () { this.running = false; };

    Flow.prototype.stop = function () {
        this.running = false;
        if (this.raf) global.cancelAnimationFrame(this.raf);
        this.raf = 0;
    };

    Flow.prototype.destroy = function () {
        this.stop();
        global.removeEventListener('resize', this._onResize);
        if (this._onClick) global.removeEventListener('click', this._onClick);
    };

    var GatewayFlow = {
        DEFAULTS: DEFAULTS,
        create: function (canvasOrId, options) {
            var el = typeof canvasOrId === 'string'
                ? document.getElementById(canvasOrId) : canvasOrId;
            if (!el) {
                if (global.console) console.warn('[GatewayFlow] 找不到画布元素');
                return null;
            }
            var inst = new Flow(el, options);
            inst.applyFilter();
            return inst;
        }
    };

    global.GatewayFlow = GatewayFlow;
})(typeof window !== 'undefined' ? window : this);
