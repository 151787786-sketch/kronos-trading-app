/*!
 * 背景管理器：三种背景之间切换，并保证永不把页面搞黑。
 *
 *   flow   —— Gateway Flow 流线（默认）：黑底 + 白色虚线汇聚到中心 + 粒子 + 点击爆破
 *   shards —— AeroShards 碎片（本地 WebGL）
 *   spline —— Spline 3D 场景（需场景地址；失败自动降级）
 *
 * 加载失败/超时/被墙 → 自动切回流线背景并给出提示。所有选择记在 localStorage；
 * 支持 URL 参数直达：?bg=flow|shards|spline&scene=<url>
 */
(function (global) {
    'use strict';

    var LS_BG = 'kronos_bg_mode';
    var LS_SCENE = 'kronos_spline_scene';
    var VALID = { tech: 1, paper: 1, galaxy: 1, cyber: 1, flow: 1, shards: 1, spline: 1 };

    function qs(name) {
        try { return new URLSearchParams(location.search || '').get(name) || ''; }
        catch (e) { return ''; }
    }

    function ls(key, def) {
        try { var v = localStorage.getItem(key); return v == null ? def : v; }
        catch (e) { return def; }
    }

    function lsSet(key, val) {
        try { localStorage.setItem(key, val); } catch (e) {}
    }

    var Bg = {
        mode: 'tech',
        scene: '',
        handle: null,
        status: 'idle',
        statusText: '',
        onStatus: null,

        init: function () {
            var m = qs('bg');
            var s = qs('scene');
            if (m) lsSet(LS_BG, m);
            if (s) lsSet(LS_SCENE, s);
            var saved = ls(LS_BG, 'tech');
            this.mode = VALID[saved] ? saved : 'tech';
            this.scene = ls(LS_SCENE, '');
            this.apply();
        },

        _report: function (state, detail) {
            this.status = state;
            this.statusText = detail || '';
            if (typeof this.onStatus === 'function') this.onStatus(state, detail);
        },

        setMode: function (mode) {
            if (!VALID[mode]) mode = 'tech';
            this.mode = mode;
            lsSet(LS_BG, mode);
            this.apply();
        },

        setScene: function (url) {
            this.scene = (url || '').trim();
            lsSet(LS_SCENE, this.scene);
            if (this.mode === 'spline') this.apply();
        },

        _layer: function (id, show) {
            var el = document.getElementById(id);
            if (el) el.style.display = show ? 'block' : 'none';
        },

        /** 显示指定层，并暂停其它层的渲染循环（省电）
         *  cyber 模式用纯 CSS 叠加层（网格/胶噪/扫描线/暗角），零 JS 开销 */
        _activate: function (which) {
            var dom = ['galaxy-bg', 'flow-bg', 'aero-bg', 'spline-bg'];
            var pb = document.getElementById('paper-bg');
            if (pb) pb.style.display = which === 'paper' ? 'block' : 'none';
            var tb = document.getElementById('tech-bg');
            if (tb) tb.style.display = which === 'tech' ? 'block' : 'none';
            dom.forEach(function (id) {
                var el = document.getElementById(id);
                if (!el) return;
                var on = (id === 'galaxy-bg' && which === 'galaxy')
                      || (id === 'flow-bg' && which === 'flow')
                      || (id === 'aero-bg' && which === 'shards')
                      || (id === 'spline-bg' && which === 'spline');
                el.style.display = on ? 'block' : 'none';
            });
            // cyber：绿色网格 + 胶噪 + 扫描线 + 暗角
            // galaxy：保留胶噪/扫描线/暗角，但去掉绿色网格（否则和星空打架）
            var sub = document.getElementById('substrate');
            if (sub) sub.style.display = which === 'cyber' ? 'block' : 'none';
            ['noise', 'scanlines', 'vignette'].forEach(function (id) {
                var el = document.getElementById(id);
                if (el) el.style.display = (which === 'cyber' || which === 'galaxy') ? 'block' : 'none';
            });
            // Gateway 模式的抖动网点只在 flow 模式显示
            var d = document.getElementById('dither');
            if (d) d.style.display = which === 'flow' ? 'block' : 'none';

            // 动效关闭时，画布类背景一律不启动渲染循环（页面彻底静止）
            var motionOff = document.documentElement.getAttribute('data-motion') === 'off';

            if (global.galaxy) {
                if (which === 'galaxy' && !motionOff) { try { global.galaxy.start(); global.galaxy.resize(); } catch (e) {} }
                else { try { global.galaxy.pause(); } catch (e) {} }
            }
            if (global.flow) {
                if (which === 'flow' && !motionOff) { try { global.flow.start(); global.flow.resize(); } catch (e) {} }
                else { try { global.flow.pause(); } catch (e) {} }
            }
            if (global.shards) {
                if (which === 'shards' && !motionOff) { try { global.shards.start(); global.shards.resize(); } catch (e) {} }
                else { try { global.shards.pause(); } catch (e) {} }
            }
        },

        _destroySpline: function () {
            if (this.handle) {
                try { this.handle.destroy(); } catch (e) {}
                this.handle = null;
            }
            var host = document.getElementById('spline-bg');
            if (host) host.innerHTML = '';
        },

        /** 任何异常都退回默认背景 —— 页面永远可用 */
        _fallback: function (reason) {
            this._destroySpline();
            this._activate('galaxy');
            this._report('error', reason);
        },

        apply: function () {
            var self = this;

            if (this.mode === 'tech') {
                this._destroySpline();
                this._activate('tech');
                this._report('ready', '科技网格');
                return;
            }

            if (this.mode === 'paper') {
                this._destroySpline();
                this._activate('paper');
                this._report('ready', '纸面背景');
                return;
            }

            if (this.mode === 'galaxy') {
                this._destroySpline();
                this._activate('galaxy');
                this._report('ready', '银河背景');
                return;
            }

            if (this.mode === 'cyber') {
                this._destroySpline();
                this._activate('cyber');
                this._report('ready', '赛博网格');
                return;
            }

            if (this.mode === 'flow') {
                this._destroySpline();
                this._activate('flow');
                this._report('ready', '流线背景');
                return;
            }

            if (this.mode === 'shards') {
                this._destroySpline();
                this._activate('shards');
                this._report('ready', '碎片背景');
                return;
            }

            // spline
            if (!this.scene) {
                this._fallback('未填写场景地址，已使用流线背景');
                return;
            }
            this._activate('spline');
            this._report('loading', '正在加载 3D 场景…');

            import('/static/spline-scene.js').then(function (mod) {
                self._destroySpline();
                self.handle = mod.SplineScene.mount(document.getElementById('spline-bg'), {
                    scene: self.scene,
                    className: 'spline-canvas',
                    onState: function (state, detail) {
                        if (state === 'ready') {
                            self._report('ready', 'Spline 3D 场景已加载');
                        } else if (state === 'error') {
                            self._fallback(detail);
                        }
                    }
                });
            }).catch(function (e) {
                self._fallback('模块加载失败：' + ((e && e.message) || e));
            });
        }
    };

    global.SplineBackground = Bg;
})(window);
