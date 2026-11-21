# fingerprint.py - 每次启动随机生成一套浏览器指纹
import json
import random


# 真实常见的 Chrome UA 池(按平台分组，配套 platform / WebGL)
# UA 里的 CR=Chrome 大版本，sec-ch-ua 会由 Chromium 自动派生
_UA_POOL = [
    {
        "platform": "Win32",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "ua_mobile": "?0",
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    {
        "platform": "Win32",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "ua_mobile": "?0",
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    {
        "platform": "Win32",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "ua_mobile": "?0",
        "webgl_vendor": "Google Inc. (Intel)",
        "webgl_renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    {
        "platform": "Win32",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "ua_mobile": "?0",
        "webgl_vendor": "Google Inc. (AMD)",
        "webgl_renderer": "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    {
        "platform": "MacIntel",
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "ua_mobile": "?0",
        "webgl_vendor": "Google Inc. (Apple)",
        "webgl_renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)",
    },
    {
        "platform": "MacIntel",
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "ua_mobile": "?0",
        "webgl_vendor": "Google Inc. (Apple)",
        "webgl_renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)",
    },
]

_HARDWARE_CONCURRENCY_POOL = [4, 6, 8, 12, 16]
_DEVICE_MEMORY_POOL = [4, 8, 16]
_COLOR_DEPTH_POOL = [24, 30]
# 屏幕逻辑分辨率池（与 window.screen 配套，独立于 viewport）
_SCREEN_LOGICAL_POOL = [
    (1920, 1080), (2560, 1440), (1680, 1050), (1440, 900),
    (1366, 768), (1536, 864), (1280, 720),
]


def generate_fingerprint() -> dict:
    """生成一套随机的、内部自洽的浏览器指纹。"""
    base = random.choice(_UA_POOL)
    hw = random.choice(_HARDWARE_CONCURRENCY_POOL)
    mem = random.choice(_DEVICE_MEMORY_POOL)
    color = random.choice(_COLOR_DEPTH_POOL)
    screen_w, screen_h = random.choice(_SCREEN_LOGICAL_POOL)

    return {
        "userAgent": base["ua"],
        "platform": base["platform"],
        "uaMobile": base["ua_mobile"],
        "webglVendor": base["webgl_vendor"],
        "webglRenderer": base["webgl_renderer"],
        "hardwareConcurrency": hw,
        "deviceMemory": mem,
        "colorDepth": color,
        "pixelRatio": random.choice([1, 1, 1.25, 1.5, 2]),
        "screenWidth": screen_w,
        "screenHeight": screen_h,
        # 小幅扰动种子，给 Canvas/Audio 指纹加噪声
        "canvasSeed": random.uniform(-0.00005, 0.00005),
        "audioSeed": random.uniform(-0.0000001, 0.0000001),
        "language": random.choice(["en-US", "en-GB", "en-US,en"]),
        "languages": ["en-US", "en"],
    }


def build_init_script(fp: dict) -> str:
    """把指纹注入成 init script（在每个页面加载前执行）。"""
    payload = json.dumps(fp, ensure_ascii=False)
    return """
    (fp => {
        const handler = {
            get(target, prop, receiver) {
                const v = Reflect.get(target, prop, receiver);
                if (typeof v === 'function') return v.bind(target);
                return v;
            }
        };

        // navigator.webdriver
        try { Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => undefined, configurable: true }); } catch(e) {}

        // user-agent / platform / languages / hardwareConcurrency / deviceMemory / pdfViewerEnabled
        try {
            const navProps = {
                userAgent: { get: () => fp.userAgent },
                appVersion: { get: () => fp.userAgent.replace('Mozilla/', '') },
                platform: { get: () => fp.platform },
                languages: { get: () => fp.languages },
                language: { get: () => fp.language },
                hardwareConcurrency: { get: () => fp.hardwareConcurrency },
                deviceMemory: { get: () => fp.deviceMemory },
            };
            for (const [k, d] of Object.entries(navProps)) {
                try { Object.defineProperty(Navigator.prototype, k, { ...d, configurable: true }); } catch(e) {}
            }
        } catch(e) {}

        // window.chrome
        try {
            window.chrome = {
                runtime: {},
                app: { isInstalled: false },
                csi: () => {},
                loadTimes: () => {},
                cast: { __proto__: null },
            };
        } catch(e) {}

        // plugins / mimeTypes（伪造非空长度，避免空指纹）
        try {
            const fakePlugin = Object.create(Plugin.prototype, {
                name: { value: 'PDF Viewer' },
                description: { value: 'Portable Document Format' },
                filename: { value: 'internal-pdf-viewer' },
                length: { value: 1 },
            });
            Object.defineProperty(Navigator.prototype, 'plugins', {
                get: () => [fakePlugin, fakePlugin, fakePlugin],
                configurable: true,
            });
            Object.defineProperty(Navigator.prototype, 'mimeTypes', {
                get: () => [{ type: 'application/pdf', suffixes: 'pdf', description: '' }],
                configurable: true,
            });
        } catch(e) {}

        // screen: width / height / colorDepth / pixelDepth / availWidth / availHeight
        try {
            const sd = Object.getOwnPropertyDescriptor(Screen.prototype, 'colorDepth');
            Object.defineProperty(screen, 'colorDepth', { get: () => fp.colorDepth, configurable: true });
            Object.defineProperty(screen, 'pixelDepth', { get: () => fp.colorDepth, configurable: true });
            Object.defineProperty(screen, 'width', { get: () => fp.screenWidth, configurable: true });
            Object.defineProperty(screen, 'height', { get: () => fp.screenHeight, configurable: true });
            Object.defineProperty(screen, 'availWidth', { get: () => fp.screenWidth, configurable: true });
            Object.defineProperty(screen, 'availHeight', { get: () => fp.screenHeight - 40, configurable: true });
            Object.defineProperty(window, 'devicePixelRatio', { get: () => fp.pixelRatio, configurable: true });
        } catch(e) {}

        // WebGL: vendor / renderer
        try {
            const origGetParameter = WebGLRenderingContext.prototype.getParameter;
            const origGetParameter2 = WebGL2RenderingContext.prototype.getParameter;
            const UNMASKED_VENDOR = 37445;
            const UNMASKED_RENDERER = 37446;
            function patched(getParameter) {
                return function(param) {
                    if (param === UNMASKED_VENDOR) return fp.webglVendor;
                    if (param === UNMASKED_RENDERER) return fp.webglRenderer;
                    return getParameter.call(this, param);
                };
            }
            WebGLRenderingContext.prototype.getParameter = patched(origGetParameter);
            WebGL2RenderingContext.prototype.getParameter = patched(origGetParameter2);
        } catch(e) {}

        // Canvas 噪声: 对 toDataURL / toBlob / getImageData 加微小扰动
        try {
            const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
            const origToBlob = HTMLCanvasElement.prototype.toBlob;
            const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;

            function tweak(data) {
                for (let i = 0; i < data.length; i += 4) {
                    // 只对非透明像素做极小扰动，避免被检测出"每像素都改"
                    if (data[i + 3] !== 0) {
                        data[i] = Math.min(255, Math.max(0, data[i] + (Math.random() < 0.5 ? 0 : 1)));
                    }
                }
                return data;
            }

            HTMLCanvasElement.prototype.toDataURL = function(...args) {
                try {
                    const ctx = this.getContext('2d');
                    if (ctx && this.width > 0 && this.height > 0) {
                        const img = origGetImageData.call(ctx, 0, 0, this.width, this.height);
                        ctx.putImageData(tweak(img), 0, 0);
                    }
                } catch(e) {}
                return origToDataURL.apply(this, args);
            };
            HTMLCanvasElement.prototype.toBlob = function(cb, ...args) {
                try {
                    const ctx = this.getContext('2d');
                    if (ctx && this.width > 0 && this.height > 0) {
                        const img = origGetImageData.call(ctx, 0, 0, this.width, this.height);
                        ctx.putImageData(tweak(img), 0, 0);
                    }
                } catch(e) {}
                return origToBlob.call(this, cb, ...args);
            };
        } catch(e) {}

        // AudioContext 噪声: 给 AnalyserNode.getByteFrequencyData 等加扰动
        try {
            const origGetChannelData = AudioBuffer.prototype.getChannelData;
            AudioBuffer.prototype.getChannelData = function(...args) {
                const data = origGetChannelData.apply(this, args);
                // 仅在首次调用时注入一次极小噪声
                if (!data.__tweaked) {
                    try {
                        for (let i = 0; i < data.length; i++) {
                            data[i] = data[i] + fp.audioSeed;
                        }
                        Object.defineProperty(data, '__tweaked', { value: true });
                    } catch(e) {}
                }
                return data;
            };
        } catch(e) {}

        // permissions API: notifications 应返回 prompt 而非 denied(被控浏览器特征)
        try {
            const origQuery = navigator.permissions.query;
            navigator.permissions.query = function(params) {
                if (params && params.name === 'notifications') {
                    return Promise.resolve({ state: 'prompt', onchange: null });
                }
                return origQuery.call(this, params);
            };
        } catch(e) {}

    })(%s);
    """ % payload
