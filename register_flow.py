# -*- coding: utf-8 -*-
"""
tencentcloud.com 国际站 - 个人账号邮箱注册（自动过 TCaptcha 点选验证码）

用法示例:
    python register_flow.py                                  # 自动建临时邮箱并完成注册
    python register_flow.py --headless
    python register_flow.py --email aaa@bbb.com --token <temp-mail-token> --password 'Xx#1234ab' --dob 1996-05-20
    python register_flow.py --port 9223 --no-close           # 指定调试端口，注册完不关浏览器

流程:
    启动 stealth chrome(带指纹注入) -> 打开 /account/register
    -> 选 Individual account -> 填邮箱/密码/出生日期/勾协议
    -> 点 Send code -> TCaptcha 弹"Select the image ..." 六宫格
    -> 截屏裁出 6 格 -> clip_guess.py(CLIP) 选格 -> 点击 + OK
    -> 轮询临时邮箱收验证码 -> 填码 -> Sign up 提交

依赖: playwright(pip install playwright)、同目录 clip_guess.py 与 clip-model/、requests
"""
from __future__ import annotations

import argparse
import atexit
import io
import json
import math
import os
import random
import re
import string
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

try:
    from fingerprint import build_init_script, generate_fingerprint
except Exception:
    build_init_script = generate_fingerprint = None

import requests  # noqa: E402
from PIL import Image  # noqa: E402

REGISTER_URL = "https://www.tencentcloud.com/account/register"
MAIL_API = "https://mail.twcdk.com"
# 本机出口代理(Clash 默认端口); 无代理环境可用环境变量 MAIL_PROXY 覆盖
# 国外网络直连即可; 需要走代理时设置环境变量 MAIL_PROXY(如 http://host:port)
LOCAL_PROXY = os.environ.get("MAIL_PROXY") or None
PROXY_CHANNELS = [LOCAL_PROXY] if LOCAL_PROXY else [None]
def _find_cloak_chrome() -> "str | None":
    """定位 cloakbrowser 安装的 chrome(python -m cloakbrowser install 装到 ~/.cloakbrowser)"""
    base = Path.home() / ".cloakbrowser"
    if not base.exists():
        return None
    cands = sorted(base.rglob("chrome.exe"))
    return str(cands[-1]) if cands else None


CHROME_EXE = os.environ.get("CHROME_EXE") or _find_cloak_chrome()

# ---------------- 与参考 run.py 一致的启动参数 ----------------
def build_launch_args(port: int, user_data_dir: str, headless: bool) -> list:
    args = [
        CHROME_EXE,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--disable-field-trial-config", "--disable-background-networking",
        "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
        "--disable-back-forward-cache", "--disable-breakpad", "--disable-client-side-phishing-detection",
        "--disable-component-extensions-with-background-pages", "--disable-component-update",
        "--no-default-browser-check", "--disable-default-apps", "--disable-dev-shm-usage",
        "--disable-edgeupdater",
        "--disable-features=AvoidUnnecessaryBeforeUnloadCheckSync,BoundaryEventDispatchTracksNodeRemoval,DestroyProfileOnBrowserClose,DialMediaRouteProvider,GlobalMediaControls,HttpsUpgrades,LensOverlay,MediaRouter,PaintHolding,ThirdPartyStoragePartitioning,Translate,AutoDeElevate,RenderDocument,OptimizationHints,msForceBrowserSignIn,msEdgeUpdateLaunchServicesPreferredVersion",
        "--enable-features=CDPScreenshotNewSurface",
        "--allow-pre-commit-input", "--disable-hang-monitor", "--disable-ipc-flooding-protection",
        "--disable-popup-blocking", "--disable-prompt-on-repost", "--disable-renderer-backgrounding",
        "--force-color-profile=srgb", "--metrics-recording-only", "--no-first-run",
        "--password-store=basic", "--use-mock-keychain", "--no-service-autorun",
        "--export-tagged-pdf", "--disable-search-engine-choice-screen",
        "--unsafely-disable-devtools-self-xss-warnings", "--edge-skip-compat-layer-relaunch",
        "--disable-infobars", "--disable-sync", "--enable-unsafe-swiftshader", "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
    ]
    # 浏览器出口代理: 验证码静态资源走境外 CDN, 直连常卡在图片区高 0
    browser_proxy = os.environ.get("BROWSER_PROXY")
    if browser_proxy:
        args.append(f"--proxy-server={browser_proxy}")
    if headless:
        args.append("--headless=new")
    args.append("about:blank")
    return args


def wait_cdp(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("chrome CDP 未就绪")


# ---------------- 临时邮箱 ----------------
class _Res:
    """极简 Response 兼容对象, 支持 .json()/.status_code/.text"""
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text[:200]}")


def _curl(url: str, method: str = "GET", payload=None, params=None, proxy=LOCAL_PROXY) -> str:
    """收信/建箱走系统 curl.exe; MAIL_PROXY 设置时经代理, 否则直连"""
    import subprocess
    if params:
        sep = "&" if "?" in url else "?"
        url += sep + "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    cmd = ["curl.exe", "-s", "--max-time", "20", "-X", method]
    if proxy:
        cmd += ["-x", proxy]
    cmd.append(url)
    if payload is not None:
        cmd += ["-H", "Content-Type: application/json",
                "-d", json.dumps(payload, ensure_ascii=False)]
    p = subprocess.run(cmd, capture_output=True, timeout=30)
    if p.returncode == 0 and p.stdout:
        return p.stdout.decode("utf-8", "ignore")
    raise RuntimeError((p.stderr or b"").decode("utf-8", "ignore")[:200] or "curl 无输出")


def _req(method: str, url: str, **kw) -> _Res:
    """顺序: curl.exe -> python 默认代理 -> python 直连;
    返回体非 JSON(如网关 400/503)同样视为失败并换通道/重试, 最多 4 轮"""
    params = kw.get("params")
    payload = kw.get("json")
    errs = []
    for _attempt in range(4):
        channels = []
        for proxy in PROXY_CHANNELS:
            try:
                raw = _curl(url, method, payload, params, proxy=proxy)
                channels.append((f"curl({proxy or 'direct'})", _Res(raw)))
            except Exception as e:
                errs.append(f"curl:{str(e)[:80]}")
        for proxy in PROXY_CHANNELS:
            try:
                kw2 = {k: v for k, v in kw.items() if k not in ("params", "json")}
                kw2.setdefault("timeout", (4, 10))
                px = {"http": proxy, "https": proxy} if proxy else None
                if method == "get":
                    r = requests.get(url, params=params, proxies=px, **kw2)
                else:
                    r = requests.post(url, json=payload, proxies=px, **kw2)
                r.raise_for_status()
                channels.append((f"py({proxy or 'direct'})", _Res(r.text, r.status_code)))
            except Exception as e:
                errs.append(f"py:{type(e).__name__}:{str(e)[:60]}")
        for name, res in channels:
            body = res.text.lstrip()
            if body.startswith("{"):
                return res
            errs.append(f"{name}:非JSON响应 {body[:40]!r}")
        time.sleep(0.8 * (_attempt + 1))
    raise RuntimeError(" | ".join(errs[-6:]))


def _get(url, **kw):
    return _req("get", url, **kw)


def _post(url, json=None, **kw):
    return _req("post", url, json=json, **kw)


def create_mailbox() -> dict:
    uname = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    return _post(f"{MAIL_API}/api/v1/addresses", json={"username": uname}).json()


def fetch_code(token: str, deadline_s: float = 360.0) -> str:
    """轮询收件箱，取最近几封里带 6 位数字验证码的邮件(不依赖主题/unread)"""
    seen = set()
    poll = 0
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        poll += 1
        try:
            r = _get(f"{MAIL_API}/api/v1/{token}/emails", params={"per_page": "30"})
            emails = r.json().get("emails", [])
            print(f"  [mail] poll#{poll} OK, {len(emails)}封", flush=True)
        except Exception as e:
            emails = []
            print(f"  [mail] poll#{poll} 失败: {str(e)[:160]}", flush=True)
        for m in emails[:5]:
            mid = m.get("id")
            if mid in seen:
                continue
            seen.add(mid)
            try:
                d = _get(f"{MAIL_API}/api/v1/{token}/emails/{mid}").json()
            except Exception:
                continue
            body = d.get("body_plain") or d.get("body_html") or ""
            hit = re.findall(r"\b(\d{6})\b", body)
            if hit:
                return hit[0]
        time.sleep(3)
    raise TimeoutError("等待验证码邮件超时")


# ---------------- 六宫格切分（与截图像素匹配） ----------------
def detect_grid_rects(bg_img: "Image.Image") -> list:
    """从 340x243 的挑战图(bg 区域)中检测白色分隔线，返回 6 个格子 (x0,y0,x1,y1) 行主序"""
    import numpy as np
    a = np.asarray(bg_img.convert("RGB")).astype(int)
    h, w, _ = a.shape
    rows = [y for y in range(h) if a[y].std() < 20]
    cols = [x for x in range(w) if a[:, x].std() < 20]

    def bands(lst):
        out = []
        for v in lst:
            if out and v - out[-1][1] <= 2:
                out[-1][1] = v
            else:
                out.append([v, v])
        return out

    br, bc = bands(rows), bands(cols)
    # 期望: 顶部留白带 + 中间横分隔带; 两条竖分隔带
    tops = [b for b in br if b[0] < 30]          # 顶部留白
    mids = [b for b in br if 80 < b[0] < 200]    # 中间分隔
    if not tops or not mids:
        raise RuntimeError(f"未检测到格子分隔线 rows={br} cols={bc}")
    y_top = tops[-1][1] + 1
    y_mid = mids[0][0] - 1
    y_bot = h - 1
    if len(bc) < 2:
        # 竖线太细检测不到时按已知显示比例(672->340, 220px 图块)推算
        bc = [[109, 115], [225, 232]]
    c1, c2 = bc[0], bc[1]
    x_edges = [0, c1[0] - 1, c1[1] + 1, c2[0] - 1, c2[1] + 1, w - 1]
    rects = []
    for r0, r1 in ((y_top, y_mid), (y_mid + 2, y_bot)):
        for i in range(3):
            rects.append((x_edges[i * 2], r0, x_edges[i * 2 + 1], r1))
    return rects


def clip_pick(image_path: str, instruction: str, regions: list) -> int:
    """调 CLIP 选格，总是返回最高分格子(1-6)。
    低置信度也照点 —— 点错服务端自动换新题，重试到过为止，比手动换题快。"""
    reg = BASE_DIR / "regions_tmp.json"
    reg.write_text(json.dumps(regions), encoding="utf-8")
    env = dict(os.environ, TX_CLIP_MODEL=str(BASE_DIR / "clip-model"))
    # 优先用常驻 daemon(快)，没有则起子进程(慢)
    try:
        req = json.dumps({"text": instruction, "image": image_path,
                          "regions": regions}).encode()
        r = urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:8701/", data=req,
            headers={"Content-Type": "application/json"}), timeout=30)
        out = json.loads(r.read().decode())
    except Exception:
        out = None
    if out is None:
        p = subprocess.run([sys.executable, str(BASE_DIR / "clip_guess.py"),
                            image_path, instruction, "--regions-json", str(reg),
                            "--json"],
                           capture_output=True, text=True, env=env, timeout=240)
        if p.returncode != 0:
            raise RuntimeError("clip_guess 失败: " + p.stderr[-500:])
        out = json.loads(p.stdout)
    scores = out.get("scores") or []
    if scores:
        return int(scores[0]["cell"])
    raise RuntimeError("CLIP 无输出")


# ---------------- 页面操作 ----------------
_DOB_SELECTORS = (
    'input[placeholder="Date of birth"]',
    '.intl-account-form__datepicker input.tea-input',
    '.tea-datepicker__input input',
)


def _dob_input(page):
    """定位生日框。表单是懒渲染的, 且不同版本 placeholder/结构有差异,
    这里按选择器优先级等第一个可见的, 避免单一定位器空转到 30s 超时。"""
    from playwright.sync_api import TimeoutError as PWTimeout
    for sel in _DOB_SELECTORS:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=8000)
            return loc
        except PWTimeout:
            continue
    shown = page.evaluate(
        "() => [...document.querySelectorAll('input')]"
        ".map(e => e.getAttribute('placeholder')).filter(Boolean)")
    raise RuntimeError(f"生日控件未出现, 页面 input: {shown}")


def pick_dob(page, dob: str) -> None:
    """日期选择器: 年视图 -> 选年 -> 月视图 -> 选月 -> 选日"""
    y, m, d = (int(x) for x in dob.split("-"))
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    dob_box = _dob_input(page)
    dob_box.click(timeout=15000)
    overlay = page.locator("#tea-overlay-root")
    # 注意: #tea-overlay-root 本身高度可能为 0，用内部 caption 判断弹层已打开
    caption = overlay.locator(".tea-calendar__caption .op-item")
    caption.first.wait_for(state="visible", timeout=8000)
    # 标题区 "Sep 2026": 点含年份的 <a>, 进入年视图
    caption.filter(has_text=re.compile(r"20\d\d")).click()
    time.sleep(0.3)
    txt = overlay.inner_text()
    # 反复翻页直到当前页包含目标年份(每页 20 年)
    for _ in range(6):
        txt = overlay.inner_text()
        mt = re.search(r"(\d{4})-(\d{4})", txt)
        if not mt:
            raise RuntimeError("年视图未出现: " + txt[:120])
        start = int(mt.group(1))
        if start <= y <= start + 19:
            break
        overlay.locator(".tea-pagination__turnbtn").first.click()
        time.sleep(0.15)
    page.get_by_role("button", name=str(y), exact=True).click()
    # 回月视图: 点标题第一个 <a>(月份名), 再点目标月
    time.sleep(0.3)
    caption.first.click()
    time.sleep(0.3)
    page.get_by_role("button", name=months[m - 1], exact=True).click()
    time.sleep(0.3)
    page.get_by_role("button", name=str(d), exact=True).click()
    time.sleep(0.3)
    val = dob_box.input_value()
    print("  [dob] 选择结果:", val)


_CAP_URL_RE = re.compile(r"captcha|tcaptcha|qcloud\.com|drag_ele", re.I)


def _cap_frames(page):
    """注册页上所有验证码相关 frame(主/备用 CDN 域名都覆盖)"""
    return [f for f in page.frames if f.url and _CAP_URL_RE.search(f.url)]


# ---------- 人类化鼠标操作(降低被风控判定为机器点击的概率) ----------
_LAST_MX = 640.0
_LAST_MY = 420.0


def _human_move(page, x: float, y: float):
    """从当前(记录)位置分段曲线移动到目标, 带随机弧线/噪声/停顿"""
    global _LAST_MX, _LAST_MY
    x0, y0 = _LAST_MX, _LAST_MY
    dist = ((x - x0) ** 2 + (y - y0) ** 2) ** 0.5
    if dist < 3:
        return
    n = max(6, min(28, int(dist / 22)))
    # 随机弧形偏移(仿人手弧线)
    arc = random.uniform(0.08, 0.22)
    for i in range(1, n + 1):
        t = i / n
        # ease-in-out
        ease = t * t * (3 - 2 * t)
        nx = x0 + (x - x0) * ease
        ny = y0 + (y - y0) * ease
        # 垂直方向小弧线 + 噪声
        wob = math.sin(t * math.pi) * arc * dist * random.uniform(-1, 1) * 0.5
        page.mouse.move(nx, ny + wob)
        time.sleep(random.uniform(0.008, 0.028))
    _LAST_MX, _LAST_MY = x, y


def _human_click(page, x: float, y: float, hold: float = 0.05):
    """移动到目标附近并带抖动点击, 模拟人手"""
    global _LAST_MX, _LAST_MY
    jx = x + random.uniform(-1.6, 1.6)
    jy = y + random.uniform(-1.6, 1.6)
    _human_move(page, jx, jy)
    time.sleep(random.uniform(0.12, 0.5))
    page.mouse.down()
    time.sleep(hold + random.uniform(0.02, 0.09))
    page.mouse.up()
    _LAST_MX, _LAST_MY = jx, jy


def captcha_pass(page) -> bool:
    """过验证码: 返回 True=通过; False=尝试次数用尽"""
    from playwright.sync_api import TimeoutError as PWTimeout
    try:
        page.wait_for_selector('iframe[src*="captcha"], iframe[src*="qcloud.com"]', timeout=25000)
    except PWTimeout:
        print("  [captcha] 未出现验证码 iframe")
        return False
    # iframe 元素插入后，对应 frame(url) 可能稍后才出现在 page.frames 里；
    # 全新 profile 首访 captcha CDN 可能较慢，最长等 ~150s，卡住就重设 src 催促
    frame = None
    deadline = time.time() + 150
    last_nudge = 0.0
    nudges = 0
    hb_last = 0.0
    while time.time() < deadline:
        caps = _cap_frames(page)
        if caps:
            frame = caps[0]
            break
        if time.time() - last_nudge > 12 and nudges < 5:
            nudges += 1
            last_nudge = time.time()
            try:
                page.evaluate(
                    "() => { const i=[...document.querySelectorAll('iframe')]"
                    ".find(x=>/captcha|qcloud\\.com/.test(x.src||'')); if(i){i.src=i.src;} }")
            except Exception:
                pass
            if os.environ.get("DBG"):
                print(f"  [dbg] nudge#{nudges} iframe src 重置", flush=True)
        if time.time() - hb_last > 15:
            hb_last = time.time()
            print(f"  [captcha] 等待验证码 frame 载入… {int(150 - (deadline - time.time()))}s",
                  flush=True)
        time.sleep(0.5)
    if frame is None:
        try:
            srcs = page.evaluate("() => [...document.querySelectorAll('iframe')].map(i => i.src)")
            print("  [captcha] iframes:", srcs[:5])
        except Exception:
            pass
        print("  [captcha] frame 未就绪")
        return False
    h0_run = 0   # 图片区未展开的连续计数(>=3 触发 iframe 重置)
    rect_fail = 0  # 切不出六宫格的连续计数(>=3 重置 iframe)
    for attempt in range(8):
        # 优先选包含挑战图(tc-bg-img)的 captcha frame
        frames = _cap_frames(page)
        frame = None
        for f in frames:
            try:
                if f.locator(".tc-bg-img").count() > 0:
                    frame = f
                    break
            except Exception:
                continue
        if frame is None:
            if not frames:
                return True  # 验证通过后 SDK 已把弹层收掉
            frame = frames[0]
        # ---- 等题型稳定 ----
        # 模板初始 body 可能是占位文案"拖动下方滑块完成拼图"(drag_ele 模板通用),
        # 挑战数据就绪后才会变成 "Select the image ..." + 六宫格, 因此不能一上来就判滑块。
        mode = None  # 'grid' | 'slider'
        for _st in range(10):
            try:
                ftxt = frame.locator("body").inner_text()
            except Exception:
                ftxt = ""
            try:
                instr_el = frame.locator(".tc-instruction-text")
                has_instr = instr_el.count() > 0 and len(instr_el.inner_text().strip()) > 0
            except Exception:
                has_instr = False
            if has_instr:
                mode = "grid"
                break
            if not re.search(r"拖动|滑块|拼图|slider|drag", ftxt):
                mode = "grid"  # 没有滑块文案也没有指令 => 按 grid 流程走, 后面读不到指令会再处理
                break
            time.sleep(1.5)
        if mode is None:
            # 12s+ 仍是滑块文案且没有 instruction => 真滑块题: 点刷新换题(最多换 4 次)
            for _sw in range(4):
                print(f"  [captcha] #{attempt+1} 滑块题型，点刷新换六宫格")
                try:
                    frame.locator(".tc-refresh-icon").first.click(timeout=2000)
                except Exception:
                    pass
                time.sleep(3)
                try:
                    instr_el = frame.locator(".tc-instruction-text")
                    if instr_el.count() > 0 and len(instr_el.inner_text().strip()) > 0:
                        mode = "grid"
                        break
                except Exception:
                    pass
        if mode != "grid":
            print("  [captcha] 持续滑块题型，暂不支持(请在有头模式下验证)")
            return False
        try:
            bh = frame.evaluate("() => { const b=document.querySelector('.tc-bg-img');"
                                " return b ? b.getBoundingClientRect().height : 0; }")
        except Exception:
            bh = 0
        if bh < 40:  # 图片还在加载/未展开: 先等待, 连续 3 次就主动重置 iframe 重新加载
            h0_run += 1
            print(f"  [captcha] #{attempt+1} 图片区未展开(h={bh}) 连续{h0_run}次，等待加载…")
            if h0_run >= 3:
                h0_run = 0
                print("  [captcha] 连续 3 次未展开，重置 iframe 重试")
                try:
                    page.evaluate(
                        "() => { const i=[...document.querySelectorAll('iframe')]"
                        ".find(x=>/captcha|qcloud/.test(x.src||''));"
                        " if(i){ const s=i.src; i.src='about:blank';"
                        " setTimeout(()=>{ i.src=s; }, 300); } }")
                except Exception:
                    pass
                time.sleep(4)
            else:
                time.sleep(4)
            continue
        h0_run = 0  # 图片区已展开, 重置连续计数
        try:
            frame.wait_for_selector(".tc-bg-img", timeout=8000)
            frame.wait_for_function(
                "() => { const b=document.querySelector('.tc-bg-img');"
                " return b && getComputedStyle(b).backgroundImage.includes('url'); }",
                timeout=8000)
        except PWTimeout:
            print(f"  [captcha] #{attempt+1} 挑战图未就绪，点刷新重试")
            try:
                fbox = page.locator('iframe[src*="captcha"], iframe[src*="qcloud.com"]').first.bounding_box()
                if fbox:
                    # 换一组按钮(frame 本地约 22,334)
                    _human_click(page, fbox["x"] + 22, fbox["y"] + 334)
            except Exception:
                pass
            time.sleep(2.5)
            continue
        # 指令文字（span 里有引号）
        try:
            instr = frame.locator(".tc-instruction-text").inner_text().strip().strip('"\u201c\u201d ')
        except Exception:
            instr = ""
        print(f"  [captcha] #{attempt+1} 题面: {instr!r}")
        if not instr:
            instr = frame.locator(".tc-popup-title").inner_text().strip()

        # 顺序点选("Click in this order: a, b, c")是多图多击题型, 本流程只解单格题;
        # 硬走六宫格逻辑只会 8 次空转, 直接换题。
        if "in this order" in instr.lower() or instr.lower().startswith("click in order"):
            print("  [captcha] 顺序点选题型(不支持)，点刷新换题")
            try:
                frame.locator(".tc-refresh-icon").first.click(timeout=2500)
            except Exception:
                pass
            time.sleep(2.5)
            continue

        # 等 loading 遮罩消失、图片真正渲染
        try:
            frame.wait_for_function(
                "() => { const l=document.querySelector('.tc-cover.tc-loading');"
                " return !l || getComputedStyle(l).display==='none'; }",
                timeout=15000)
        except PWTimeout:
            pass

        # 截屏 -> 裁出 iframe 内 bg 图区域(frame 本地 (10,72) 起 340x243)
        rects = None
        for _try in range(3):
            box = page.locator('iframe[src*="captcha"]').bounding_box()
            if not box:
                continue
            shot_bytes = page.screenshot()
            img = Image.open(io.BytesIO(shot_bytes)).convert("RGB")
            bg = img.crop((int(box["x"]) + 10, int(box["y"]) + 72,
                           int(box["x"]) + 10 + 340, int(box["y"]) + 72 + 243))
            bg_path = BASE_DIR / "captcha_cur.png"
            bg.save(bg_path)
            try:
                rects = detect_grid_rects(bg)
                break
            except RuntimeError:
                time.sleep(1.5)
        if rects is None:
            rect_fail += 1
            print(f"  [captcha] 图片区域仍未渲染好({rect_fail}/3)，重截一次")
            if rect_fail >= 3:
                print("  [captcha] 连续切不出格子，重置 captcha iframe")
                try:
                    page.evaluate(
                        "() => { const i=[...document.querySelectorAll('iframe')]"
                        ".find(x=>/captcha|qcloud/.test(x.src||''));"
                        " if(i){ const s=i.src; i.src='about:blank';"
                        " setTimeout(()=>{ i.src=s; }, 300); } }")
                except Exception:
                    pass
                rect_fail = 0
            time.sleep(2.5)
            continue
        rect_fail = 0
        cell = clip_pick(str(bg_path), instr, rects)  # 永远点最高分格, 错了自动换题
        x0, y0, x1, y1 = rects[cell - 1]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        px = box["x"] + 10 + cx
        py = box["y"] + 72 + cy
        print(f"  [captcha] CLIP 选择格子 {cell}，点击 ({px:.0f},{py:.0f})")
        time.sleep(random.uniform(0.9, 2.0))  # 人类读题时间
        _human_click(page, px, py)
        time.sleep(random.uniform(1.4, 2.4))  # 点完观察一下再去找 OK

        def _bg_hash():
            try:
                return frame.evaluate(
                    "() => { const b=document.querySelector('.tc-bg-img');"
                    " const u=getComputedStyle(b).backgroundImage;"
                    " const m=u.match(/image=[0-9a-f]+/); return m ? m[0] : ''; }")
            except Exception:
                return ""

        def _click_ok():
            try:
                ok = frame.locator(".tc-verify-button").first
                ok.wait_for(state="visible", timeout=4000)
                okb = ok.bounding_box()
                # 注意: 跨域 frame 的 bounding_box 返回的是主页面坐标, 直接点中心即可
                if okb and okb["width"] > 10 and okb["height"] > 10:
                    _human_click(page, okb["x"] + okb["width"] / 2,
                                 okb["y"] + okb["height"] / 2)
                    print(f"  [captcha] OK 已点击 @page({okb['x'] + okb['width'] / 2:.0f},"
                          f"{okb['y'] + okb['height'] / 2:.0f})")
                    return True
            except Exception:
                pass
            try:  # 兜底: JS 直接触发(最后手段)
                frame.evaluate("() => { const b=document.querySelector('.tc-verify-button');"
                               " if (b) b.click(); }")
                print("  [captcha] OK JS 点击兜底")
                return True
            except Exception:
                return False

        h0 = _bg_hash()
        _click_ok()
        time.sleep(4)
        try:
            text = frame.locator("body").inner_text()
        except Exception:
            text = ""
        if "Verification passed" not in text and "verified" not in text.lower():
            h1 = _bg_hash()
            if h1 and h1 == h0:  # 服务端没换题 -> 大概率 verify 被 abort, 补点一次
                print("  [captcha] 结果未知且图未换(hash 相同)，补点一次 OK")
                _click_ok()
                time.sleep(4)
                try:
                    text = frame.locator("body").inner_text()
                except Exception:
                    text = ""
        if "Verification passed" in text or "verified" in text.lower():
            print("  [captcha] ✔ 验证通过")
            time.sleep(1.2)
            try:  # 关掉成功弹层
                ok = frame.get_by_role("button", name="OK")
                ok.first.click(timeout=2500)
            except Exception:
                pass
            time.sleep(1.5)
            return True
        # 风控/环境类提示(答对也被判环境风险时出现), 识别后点刷新换题重来
        if re.search(r"environment|risk|abnormal|secure|verif(y|ication).*fail|"
                     r"异常|风险|环境|超时|timeout|try again|网络",
                     text, re.I):
            print("  [captcha] ⚠ 检测到风控/环境提示，点刷新换题重试:", text[:90])
            try:
                frame.locator(".tc-refresh-icon").first.click(timeout=2500)
            except Exception:
                try:
                    fbox = page.locator('iframe').first.bounding_box()
                    if fbox:
                        _human_click(page, fbox["x"] + 22, fbox["y"] + 334)
                except Exception:
                    pass
            time.sleep(3.5)
            continue
        print("  [captcha] ✘ 未通过(可能答错)，服务端已换新题，继续尝试")
        time.sleep(1.2)
    return False


# ---------------- 退出自动清理(每次流程结束删除浏览器 profile) ----------------
_PROC = None   # 本流程拉起的 chrome 进程
_UD = None     # 本流程 profile 目录
_KEEP = False  # --no-close / --keep-profile 时保留


def _rmtree_retry(path: str):
    import shutil
    for _ in range(6):
        try:
            shutil.rmtree(path, ignore_errors=False)
            return True
        except Exception:
            time.sleep(1.0)
    shutil.rmtree(path, ignore_errors=True)
    return False


def _auto_cleanup():
    if _KEEP:
        return
    if _PROC is not None:
        try:
            _PROC.terminate()
        except Exception:
            pass
        time.sleep(1.2)
    if _UD:
        _rmtree_retry(_UD)


atexit.register(_auto_cleanup)


# ---------------- EdgeOne Makers API Key ----------------
KEY_FILE = BASE_DIR / "key.txt"

# ---- 上游单条 key 上传(地址/令牌由 Actions 传入, 不写死在代码) ----
# 地址: workflow 触发时填 upstream(host:port); 令牌: Actions Secret UPSTREAM_TOKEN
# 本地运行可用环境变量 UPSTREAM_API / UPSTREAM_TOKEN 代替


def upload_key(name: str, key: str, remark: str = "tencentcloud intl",
               priority: int = 0, weight: int = 1, quota_limit_manual: int = 500000,
               upstream: str | None = None) -> bool:
    """单条上传一个 key; 成功(2xx)返回 True。不打印 key 本身与响应体。
    upstream 支持 host:port 或 http://host:port, 缺省读 UPSTREAM_API。"""
    payload = json.dumps({"name": name, "key": key, "priority": priority,
                          "weight": weight, "quota_limit_manual": quota_limit_manual,
                          "remark": remark}, ensure_ascii=False)
    host = (upstream or os.environ.get("UPSTREAM_API") or "").strip().rstrip("/")
    # workflow 的 upstream 输入是 host:port 形式(README 例子 198.44.182.213:8081)。
    # curl 会自动补 http, requests 不会, 缺 scheme 会直接抛
    # "No connection adapters were found" -> 补成 http://。
    if host and "://" not in host:
        host = "http://" + host
    tok = os.environ.get("UPSTREAM_TOKEN") or ""
    if not host or not tok:
        print("  [upstream] 未配置上游地址/令牌(UPSTREAM_API/UPSTREAM_TOKEN), 跳过上传",
              flush=True)
        return False
    # 用 requests 而不是 curl.exe: Windows 的 curl.exe 不认 /dev/null(-o 会把路径
    # 当文件名), 老实现下 %{http_code} 恒为空, 每个 key 都被误判为上传失败。
    url = host + "/api/upstream-keys"
    try:
        r = requests.post(url,
                          headers={"Authorization": f"Bearer {tok}",
                                   "Content-Type": "application/json"},
                          data=payload.encode("utf-8"), timeout=30)
        ok = 200 <= r.status_code < 300
        print(f"  [upstream] {name} HTTP {r.status_code}"
              + ("" if ok else f" err:{r.text[:120]}"), flush=True)
        return ok
    except Exception as e:
        print(f"  [upstream] 上传异常: {str(e)[:120]}", flush=True)
        return False

_MAKERS_URL = "https://console.tencentcloud.com/edgeone/makers"
_MAKERS_KEY_URL = "https://console.tencentcloud.com/edgeone/makers?tab=models&subTab=apikey"
# 用户实测 XPath(控制台布局, 可能随版本变化; 失败会打印截图与页面片段便于适配)
_XP_A = "/html/body/div[2]/div[2]/div[2]/div/section/section/main/div/div[2]/div/div[2]/div/div[3]/div/section/main/div/div/div/div/div/div[1]/div[4]/button[1]"
_XP_B = "/html/body/div[2]/div[2]/div[2]/div/section/section/main/div/div[2]/div/div[2]/div/div[3]/div/section/section/main/div/div/div/div/div[2]/div[2]/div/div[2]/div/div/div[1]/div/div[1]/button"
_XP_INPUT = "/html/body/div[7]/div/div[3]/div/div[2]/div/div[1]/div[2]/input"
_XP_BTN = "/html/body/div[7]/div/div[3]/div/div[3]/div/button[1]"
_XP_KEY = "/html/body/div[7]/div/div[3]/div/div[2]/div[1]/span"


def _xp(locator_text: str):
    return "xpath=" + locator_text


def _wait_visible(page, xp, timeout_s: float, desc: str):
    loc = page.locator(_xp(xp))
    try:
        loc.first.wait_for(state="visible", timeout=timeout_s * 1000)
        return loc.first
    except Exception:
        raise RuntimeError(f"{desc} 未出现(timeout={timeout_s}s)")


def _dump_state(page, tag: str):
    try:
        png = BASE_DIR / f"keyfail_{tag}.png"
        page.screenshot(path=str(png), full_page=False)
        print(f"  [key] 截图已存: {png.name}")
        txt = page.evaluate("() => document.body.innerText.replace(/\\s+/g,' ').slice(0,300)")
        print(f"  [key] 页面片段: {txt[:200]}")
    except Exception:
        pass


def fetch_makers_key(page, wait_after: float = 8.0, key_file=None) -> str:
    """注册完成后在控制台创建 EdgeOne Makers API Key, 追加写入 key.txt 并返回"""
    key_path = Path(key_file) if key_file else KEY_FILE
    def wait_load(extra: float = 0.0):
        page.wait_for_load_state("domcontentloaded")
        time.sleep(wait_after + extra)

    # 1. 打开 makers 首页
    print("  [key] 打开 makers…")
    page.goto(_MAKERS_URL, wait_until="domcontentloaded", timeout=90000)
    _wait_visible(page, _XP_A, 60, "按钮A(首页)")
    # 2. 点按钮 A
    page.locator(_xp(_XP_A)).first.click()
    print("  [key] 已点按钮A，等待加载…")
    time.sleep(wait_after)
    # 3. 刷新当前页并等待
    page.reload(wait_until="domcontentloaded")
    time.sleep(wait_after)
    # 4. 打开 apikey 子页
    print("  [key] 打开 apikey 页…")
    page.goto(_MAKERS_KEY_URL, wait_until="domcontentloaded", timeout=90000)
    _wait_visible(page, _XP_B, 60, "按钮B(创建Key)")
    # 5. 点按钮 B -> 弹窗
    page.locator(_xp(_XP_B)).first.click()
    print("  [key] 已点按钮B，等弹窗…")
    _wait_visible(page, _XP_INPUT, 30, "弹窗输入框")
    # 6. 输入固定值 1
    page.locator(_xp(_XP_INPUT)).first.fill("1")
    # 7. 点弹窗确认按钮
    page.locator(_xp(_XP_BTN)).first.click()
    print("  [key] 已提交，等待 key 生成…")
    # 8. 等 span 出现并取值(sk- 开头)
    span = None
    for _ in range(60):
        try:
            el = page.locator(_xp(_XP_KEY)).first
            if el.is_visible():
                v = (el.inner_text() or "").strip()
                if v.startswith("sk-"):
                    span = v
                    break
        except Exception:
            pass
        time.sleep(1.5)
    if not span:
        _dump_state(page, "span")
        raise RuntimeError("key span 未出现")
    with open(key_path, "a", encoding="utf-8") as f:
        f.write(span + "\n")
    print(f"  [key] ✔ sk-******{span[-6:]} (已写入 {key_path})")
    return span


def main() -> int:
    ap = argparse.ArgumentParser(description="Tencent Cloud Intl 个人邮箱注册(自动过验证码)")
    ap.add_argument("--chrome-exe", default=CHROME_EXE)
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--email", default=None, help="固定邮箱(否则自动建临时邮箱)")
    ap.add_argument("--token", default=None, help="临时邮箱 token(固定邮箱时用于收码)")
    ap.add_argument("--password", default=None)
    ap.add_argument("--dob", default="1996-05-20")
    ap.add_argument("--no-close", action="store_true", help="注册完不关浏览器")
    ap.add_argument("--keep-profile", action="store_true", help="保留浏览器 profile 目录")
    ap.add_argument("--no-key", action="store_true", help="跳过 EdgeOne Makers 领 Key")
    ap.add_argument("--key-wait", type=float, default=8.0, help="控制台每步等待秒数")
    ap.add_argument("--upstream", default=os.environ.get("UPSTREAM_API", ""),
                    help="上游上传地址 http://host:port(默认读 UPSTREAM_API 环境变量)")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    # 1. 建邮箱 / 收码凭证
    token = args.token
    email = args.email
    if not email:
        mb = create_mailbox()
        email, token = mb["email"], mb["token"]
        print("临时邮箱:", email, "到期:", mb.get("expires_at"))
        print("mail-token:", token)
    # 收信通道自检(MAIL_PROXY 若设置则经代理)
    try:
        r0 = _get(f"{MAIL_API}/api/v1/domains")
        print("[mail] 通道自检 OK:", r0.text[:60], flush=True)
    except Exception as e:
        print("[mail] 通道自检失败:", str(e)[:150], flush=True)
    password = args.password or ("Tc#" + str(random.randint(10, 99)) +
                                 "".join(random.choices(string.ascii_lowercase, k=6)) + "!x")
    print("密码:", password)

    # 2. 启动 stealth chrome
    ud = BASE_DIR / f"ud_{int(time.time()*1000)}"
    ud.mkdir(parents=True, exist_ok=True)
    print("启动 chrome:", args.chrome_exe)
    global _PROC, _UD, _KEEP
    proc = subprocess.Popen(build_launch_args(args.port, str(ud), args.headless))
    wait_cdp(args.port)
    _PROC, _UD, _KEEP = proc, str(ud), bool(args.no_close or args.keep_profile)
    if generate_fingerprint:
        fp = generate_fingerprint()
        init_js = build_init_script(fp)
        print("指纹:", fp["platform"], fp["userAgent"][:60])
    else:
        init_js = ""

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
        ctx = browser.contexts[0]
        if init_js:
            ctx.add_init_script(init_js)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.set_viewport_size({"width": 1440, "height": 900})
        except Exception:
            pass

        # 3. 打开注册页
        page.goto(REGISTER_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_selector("text=Sign up for a Tencent Cloud account", timeout=30000)
        print("注册页已打开")

        # 4. 选 Individual account
        try:
            page.get_by_text("Individual account", exact=True).first.click(timeout=8000)
        except Exception:
            page.locator("main").get_by_text("Individual account").first.click()
        time.sleep(0.6)

        # 5. 填表
        page.get_by_role("textbox", name="Enter your personal email").fill(email)
        page.get_by_role("textbox", name="Enter your login password").fill(password)
        page.get_by_role("textbox", name="Please confirm your login").fill(password)
        pick_dob(page, args.dob)
        for cb in page.get_by_role("checkbox").all():
            if not cb.is_checked():
                cb.check()
        print("表单已填写")

        # 6. 发送验证码(触发 TCaptcha)
        send = page.get_by_role("button", name="Send code")
        send.wait_for(state="visible", timeout=8000)
        if send.is_disabled():
            raise RuntimeError("Send code 不可用(表单校验未过?)")
        send.click()
        print("已点 Send code，等待验证码…")

        # 7. 过验证码
        if not captcha_pass(page):
            print("验证码多次失败，结束(浏览器保留便于观察)")
            if not args.no_close:
                pass
            return 2

        # 7.5 确认主机页已触发发码(按钮应进入倒计时), 便于失败时定位
        try:
            for _ in range(6):
                time.sleep(1.5)
                btns = page.evaluate(
                    "() => [...document.querySelectorAll('button')]"
                    ".filter(b => /send code|resend|\\d+ s/i.test((b.innerText||'').trim()))"
                    ".map(b => b.innerText.trim()).slice(0,3)")
                if any(re.search(r"\d+\s*s", t or "") for t in btns):
                    print("  [send] 发码倒计时:", btns)
                    break
            else:
                print("  [send] 未见倒计时按钮，页面按钮状态:", btns)
        except Exception as e:
            print("  [send] 状态检查跳过:", str(e)[:100])

        # 8. 等邮件验证码
        print("等待邮件验证码…")
        code = None
        if token:
            try:
                code = fetch_code(token)
            except TimeoutError:
                print("\n[mail] 自动收信超时 —— 可手动查信:")
                print(f'  curl "https://mail.twcdk.com/api/v1/{token}/emails"')
                print("  再取详情 curl .../emails/{id} 看正文 6 位码")
        if not code:
            code = input("请输入收到的验证码(可先 curl 邮箱查看): ").strip()
        print("验证码:", code)

        # 9. 填码提交
        page.get_by_role("textbox", name="Enter the code").fill(code)
        time.sleep(0.5)
        page.get_by_role("button", name="Sign up", exact=True).click()
        try:
            page.wait_for_url(re.compile(r"/register/completion"), timeout=60000)
            ok = True
        except Exception:
            ok = "Complete information" in page.inner_text("body")
        if ok:
            print("\n=== 注册成功 ===")
            print("email   :", email)
            print("password:", password)
            if token:
                print("mail-token:", token)
        else:
            print("提交后未见成功页，请人工查看浏览器(URL=%s)" % page.url)

        # 10. (可选) 去 EdgeOne Makers 领 API Key
        key = None
        if ok and not args.no_key:
            try:
                key = fetch_makers_key(page, wait_after=args.key_wait)
                print("API Key: sk-******" + key[-6:] if key else "API Key: 无")
                if key:
                    upload_key(email, key, upstream=args.upstream or None)
            except Exception as e:
                print("领 Key 失败(不影响注册):", str(e)[:200])
        if not args.no_close:
            browser.close()
    if not args.no_close:
        proc.terminate()
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
