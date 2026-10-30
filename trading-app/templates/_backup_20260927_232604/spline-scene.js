/*!
 * SplineScene — 把 React 版 <SplineScene scene={...} className={...} /> 适配到本项目的 vanilla 环境
 *
 * 你给的原组件：
 *   'use client'
 *   import { Suspense, lazy } from 'react'
 *   const Spline = lazy(() => import('@splinetool/react-spline'))
 *   export function SplineScene({ scene, className }) { ... <Spline scene={scene} className={className} /> }
 *
 * 本项目的约束：单个 index.html + 原生 JS，没有 React、没有打包器、没有 Tailwind。
 * 因此这里**不使用** @splinetool/react-spline（那只是 React 的薄包装），而是直接用它的底层
 * 渲染引擎 @splinetool/runtime（框架无关，同样导出 Application），渲染结果完全一致：
 *
 *   React 版：  <Spline scene={url} className={cls} />
 *   等价写法：  new Application(canvas).load(url)
 *
 * 好处：不需要引入 React（省 ~150KB）、不需要 CDN、不依赖任何构建步骤。
 *
 * 依赖已下载到本地：static/vendor/spline-runtime.js（约 2MB，自包含 ESM）
 *
 * 用法：
 *   import { SplineScene } from '/static/spline-scene.js';
 *   const handle = SplineScene.mount(containerEl, {
 *       scene: 'https://prod.spline.design/xxxx/scene.splinecode',
 *       className: 'spline-canvas',
 *       onState: (state, detail) => { ... }   // loading | ready | error
 *   });
 *   handle.destroy();
 */
import { Application } from './vendor/spline-runtime.js';

var LOAD_TIMEOUT_MS = 15000;

function ensureStyles() {
    if (document.getElementById('spline-scene-style')) return;
    var css = [
        '.spline-scene{position:relative;width:100%;height:100%;overflow:hidden}',
        '.spline-canvas{width:100%;height:100%;display:block;outline:none}',
        /* 对应原组件 Suspense 的 fallback 容器：
           "w-full h-full flex items-center justify-center"（Tailwind）→ 等价 CSS */
        '.spline-fallback{width:100%;height:100%;display:flex;align-items:center;justify-content:center}',
        /* .loader 转圈（原组件引用了这个类名） */
        '.loader{width:28px;height:28px;border:3px solid rgba(168,85,247,.25);',
        '  border-top-color:#A855F7;border-radius:50%;animation:spline-spin .8s linear infinite;display:inline-block}',
        '@keyframes spline-spin{to{transform:rotate(360deg)}}',
        '.spline-msg{color:#9A90B4;font-size:12px;margin-top:10px;text-align:center;line-height:1.6;padding:0 16px}'
    ].join('\n');
    var el = document.createElement('style');
    el.id = 'spline-scene-style';
    el.textContent = css;
    document.head.appendChild(el);
}

function SplineScene(props) {
    ensureStyles();
    var opts = props || {};
    var scene = opts.scene;
    var className = opts.className || 'spline-canvas';
    var onState = typeof opts.onState === 'function' ? opts.onState : function () {};

    var root = document.createElement('div');
    root.className = 'spline-scene ' + (opts.wrapperClassName || '');

    // 与 React 版 <Suspense fallback={...}> 等价：先渲染 loader，加载完成后替换
    var fallback = document.createElement('div');
    fallback.className = 'spline-fallback';
    fallback.innerHTML = '<div style="text-align:center"><span class="loader"></span>' +
        '<div class="spline-msg">正在加载 3D 场景…</div></div>';
    root.appendChild(fallback);

    var canvas = document.createElement('canvas');
    canvas.className = className;
    canvas.style.display = 'none';

    var app = null;
    var destroyed = false;
    var timer = null;

    function fail(msg) {
        if (destroyed) return;
        onState('error', msg);
        fallback.innerHTML = '<div class="spline-msg">⚠️ 3D 场景加载失败<br>' +
            String(msg).slice(0, 160) + '<br><span style="opacity:.7">已自动切回本地碎片背景</span></div>';
    }

    function start() {
        if (!scene) {
            fail('未配置场景地址');
            return;
        }
        root.appendChild(canvas);
        try {
            app = new Application(canvas);
        } catch (e) {
            fail('WebGL 初始化失败：' + e.message);
            return;
        }
        onState('loading', scene);

        timer = setTimeout(function () {
            if (!destroyed) fail('加载超时（15 秒）——场景 CDN 可能被墙或地址有误');
        }, LOAD_TIMEOUT_MS);

        app.load(scene).then(function () {
            if (destroyed) return;
            clearTimeout(timer);
            fallback.style.display = 'none';
            canvas.style.display = 'block';
            onState('ready', scene);
        }).catch(function (e) {
            if (destroyed) return;
            clearTimeout(timer);
            fail((e && e.message) || String(e));
        });
    }

    start();

    return {
        el: root,
        app: function () { return app; },
        destroy: function () {
            destroyed = true;
            clearTimeout(timer);
            try { if (app && typeof app.dispose === 'function') app.dispose(); } catch (e) {}
            try { if (app && typeof app.stop === 'function') app.stop(); } catch (e) {}
            root.remove();
        }
    };
}

SplineScene.mount = function (container, props) {
    var handle = SplineScene(props);
    if (container) {
        container.innerHTML = '';
        container.appendChild(handle.el);
    }
    return handle;
};

SplineScene.LOAD_TIMEOUT_MS = LOAD_TIMEOUT_MS;
export { SplineScene };
export default SplineScene;
