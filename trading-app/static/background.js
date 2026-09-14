/*!
 * 背景管理器：在「本地碎片动画」与「Spline 3D 场景」之间切换，并保证永不把页面搞黑。
 *
 *   - 默认 = 本地碎片动画（AeroShards，零网络依赖，永远可用）
 *   - 选 Spline 时必须提供场景地址；加载失败/超时/被墙 → 自动切回碎片动画并给出提示
 *   - 所有选择记在 localStorage；支持 URL 参数直达：?bg=shards|spline&scene=<url>
 */
(function (global) {
    'use strict';

    var LS_BG = 'kronos_bg_mode';
    var LS_SCENE = 'kronos_spline_scene';

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
        mode: 'shards',
        scene: '',
        handle: null,
        status: 'idle',      // idle | loading | ready | error
        statusText: '',
        onStatus: null,

        init: function () {
            var m = qs('bg');
            var s = qs('scene');
            if (m) lsSet(LS_BG, m);
            if (s) lsSet(LS_SCENE, s);
            this.mode = ls(LS_BG, 'shards');
            this.scene = ls(LS_SCENE, '');
            this.apply();
        },

        _report: function (state, detail) {
            this.status = state;
            this.statusText = detail || '';
            if (typeof this.onStatus === 'function') this.onStatus(state, detail);
        },

        setMode: function (mode) {
            this.mode = mode;
            lsSet(LS_BG, mode);
            this.apply();
        },

        setScene: function (url) {
            this.scene = (url || '').trim();
            lsSet(LS_SCENE, this.scene);
            if (this.mode === 'spline') this.apply();
        },

        _showShards: function (show) {
            var c = document.getElementById('aero-bg');
            if (c) c.style.display = show ? 'block' : 'none';
            if (show && global.shards && !global.shards.running) {
                try { global.shards.start(); } catch (e) {}
            } else if (!show && global.shards) {
                try { global.shards.pause(); } catch (e) {}
            }
        },

        _fallbackToShards: function (reason) {
            this._destroySpline();
            this._showShards(true);
            this._report('error', reason);
        },

        _destroySpline: function () {
            if (this.handle) {
                try { this.handle.destroy(); } catch (e) {}
                this.handle = null;
            }
            var host = document.getElementById('spline-bg');
            if (host) host.innerHTML = '';
        },

        apply: function () {
            var self = this;
            if (this.mode !== 'spline') {
                this._destroySpline();
                this._showShards(true);
                this._report('ready', '本地碎片动画');
                return;
            }
            if (!this.scene) {
                this._fallbackToShards('未填写场景地址，已使用本地碎片背景');
                return;
            }
            this._showShards(false);
            var host = document.getElementById('spline-bg');
            if (!host) {
                this._fallbackToShards('缺少容器');
                return;
            }
            this._report('loading', '正在加载 3D 场景…');

            import('/static/spline-scene.js').then(function (mod) {
                self._destroySpline();
                self.handle = mod.SplineScene.mount(host, {
                    scene: self.scene,
                    className: 'spline-canvas',
                    onState: function (state, detail) {
                        if (state === 'ready') {
                            self._report('ready', 'Spline 3D 场景已加载');
                        } else if (state === 'error') {
                            self._fallbackToShards(detail);
                        }
                    }
                });
            }).catch(function (e) {
                self._fallbackToShards('模块加载失败：' + ((e && e.message) || e));
            });
        }
    };

    global.SplineBackground = Bg;
})(window);
