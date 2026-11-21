# -*- coding: utf-8 -*-
"""
批量连续注册 tencentcloud.com 国际站个人账号(多进程并发版)

为什么是多进程: playwright sync API 在多线程内不可靠(事件循环可能挂起),
每路注册使用独立 python 进程 -> 互不干扰, 任一进程崩溃不影响其它路。

速度优化:
  1. CLIP 模型常驻(父进程拉起 clip_daemon.py) —— 所有子进程共享, 每题 2~3s
  2. --workers N: N 个并发进程, 每个进程注册 --count 个(默认 2 x 10)
  3. 每单全新 profile + 随机指纹 + 独立 CDP 端口(按 worker 分段)
  4. 单账号失败即换下一个邮箱(默认只试 1 次); 每个 worker 补位到凑齐 --count 个成功

用法:
  python -u batch_register.py --count 10                # 1 进程 x 10
  python -u batch_register.py --count 10 --workers 2    # 2 进程 x 10 = 20 并发
  python -u batch_register.py --count 10 --workers 2 --headless
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import string
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
import register_flow as rf  # noqa: E402


def _new_password() -> str:
    return "Tc#" + str(random.randint(10, 99)) + \
        "".join(random.choices(string.ascii_lowercase, k=6)) + "!x"


def register_one(tag: int, headless: bool, mailbox: dict | None,
                 dob: str, fetch_deadline: float, port: int,
                 want_key: bool = True, key_file=None,
                 upstream: str | None = None) -> dict:
    """注册单个账号(+可选领 EdgeOne API Key), 返回结果 dict"""
    from playwright.sync_api import sync_playwright

    email = mailbox["email"]
    token = mailbox["token"]
    password = _new_password()
    rec = {"idx": tag, "email": email, "password": password,
           "mail_token": token, "status": "pending"}

    ud = BASE_DIR / f"batch_ud_{tag}_{int(time.time() * 1000)}"
    ud.mkdir(parents=True, exist_ok=True)
    exe = rf.CHROME_EXE
    if not exe:
        raise RuntimeError("未找到 chrome(设 CHROME_EXE 环境变量或先 python -m cloakbrowser install)")
    if not os.path.exists(exe):
        raise RuntimeError(f"chrome 路径不存在: {exe!r}")
    proc = subprocess.Popen(rf.build_launch_args(port, str(ud), headless))
    try:
        rf.wait_cdp(port)
        fp = rf.generate_fingerprint()
        init_js = rf.build_init_script(fp)
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            ctx.add_init_script(init_js)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.set_viewport_size({"width": 1440, "height": 900})
            except Exception:
                pass

            page.goto(rf.REGISTER_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_selector("text=Sign up for a Tencent Cloud account", timeout=30000)

            page.get_by_text("Individual account", exact=True).first.click(timeout=8000)
            # 个人账号表单是懒渲染的: 先等密码框落地, 否则后面的生日控件会空转到超时
            page.wait_for_selector('input[name="password"]', state="visible", timeout=25000)
            page.get_by_role("textbox", name="Enter your personal email").fill(email)
            page.get_by_role("textbox", name="Enter your login password").fill(password)
            page.get_by_role("textbox", name="Please confirm your login").fill(password)
            rf.pick_dob(page, dob)
            for cb in page.get_by_role("checkbox").all():
                if not cb.is_checked():
                    cb.check()

            send = page.get_by_role("button", name="Send code")
            send.wait_for(state="visible", timeout=8000)
            if send.is_disabled():
                raise RuntimeError("Send code 不可用")
            send.click()

            if not rf.captcha_pass(page):
                raise RuntimeError("验证码多次未通过")

            code = rf.fetch_code(token, deadline_s=fetch_deadline)
            page.get_by_role("textbox", name="Enter the code").fill(code)
            time.sleep(0.4)
            page.get_by_role("button", name="Sign up", exact=True).click()
            try:
                page.wait_for_url(re.compile(r"/register/completion"), timeout=60000)
            except Exception:
                body = page.inner_text("body")
                if "Complete information" not in body:
                    raise RuntimeError("提交后未见成功页")
            if want_key:
                try:
                    key = rf.fetch_makers_key(page, key_file=key_file)
                    rec["key"] = key
                    if key:
                        has_upstream = bool(upstream or os.environ.get(
                            "UPSTREAM_API")) and bool(os.environ.get("UPSTREAM_TOKEN"))
                        if not has_upstream:
                            rec["upload"] = "skip"  # 未配置上游, 不算失败
                        else:
                            rec["upload"] = ("ok" if rf.upload_key(
                                email, key, upstream=upstream) else "fail")
                except Exception as e:
                    print(f"    [key] 领 Key 失败: {str(e)[:160]}", flush=True)
                    rec["key"] = None
            rec["status"] = "ok"
            rec["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
            browser.close()
    except Exception as e:
        rec["status"] = "fail"
        rec["error"] = str(e)[:300]
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            import shutil
            shutil.rmtree(ud, ignore_errors=True)
        except Exception:
            pass
    return rec


def _daemon_up(timeout_s: float) -> bool:
    """探测 8701 是否已监听(daemon 就绪即模型已加载完)"""
    import urllib.error
    import urllib.request
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            urllib.request.urlopen("http://127.0.0.1:8701/", timeout=1.0)
            return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            time.sleep(1)
    return False


def _worker_proc(wid: int, count: int, tries: int, headless: bool, dob: str,
                 mail_timeout: float, base_port: int, max_mail: int,
                 want_key: bool, upstream: str | None = None) -> int:
    """单进程 worker: 补位注册到凑齐 count 个成功, 返回成功数"""
    ok = fail = 0
    local = 0
    key_file = str(BASE_DIR / f"key_w{wid}.txt") if want_key else None
    if key_file and os.path.exists(key_file):
        os.remove(key_file)
    while ok < count and local < (max_mail or count * 2):
        local += 1
        t0 = time.time()
        try:
            mail = rf.create_mailbox()
        except Exception as e:
            print(f"[w{wid}] 建箱失败: {str(e)[:120]}", flush=True)
            time.sleep(2)
            continue
        port = base_port + wid * 100 + local
        _m = mail["email"]
        _mask = (_m.split("@")[0][:3] + "***@" + _m.split("@")[-1]) if "@" in _m else "***"
        print(f"[w{wid}] 目标 {ok+1}/{count} 邮箱#{local} {_mask} 端口{port}", flush=True)
        rec = None
        for try_i in range(1, tries + 1):
            print(f"[w{wid}]   尝试 {try_i}/{tries}…", flush=True)
            rec = register_one(port, headless, mail, dob, mail_timeout, port,
                               want_key=want_key, key_file=key_file, upstream=upstream)
            if rec["status"] == "ok":
                break
            print(f"[w{wid}]   ✘ {rec.get('error','')[:180]}", flush=True)
            if try_i < tries:
                try:
                    mail = rf.create_mailbox()
                except Exception:
                    pass
        if rec and rec["status"] == "ok":
            ok += 1
            rec["idx"] = ok
            rec["worker"] = wid
            print(f"[w{wid}]   ✔ #{ok} {rec['email']} 用时 {time.time()-t0:.0f}s", flush=True)
        else:
            fail += 1
            if rec is None:
                rec = {"idx": None, "status": "fail", "error": "建箱失败"}
            rec["idx"] = None
            rec["worker"] = wid
            print(f"[w{wid}]   ⏱ {time.time()-t0:.0f}s 失败跳过", flush=True)
        with open(str(BASE_DIR / f"accounts_w{wid}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[w{wid}] 结束: 成功 {ok}/{count} 失败 {fail}", flush=True)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="批量注册 Tencent Cloud Intl 账号(多进程并发)")
    ap.add_argument("--count", type=int, default=10, help="每个 worker 进程注册账号数")
    ap.add_argument("--workers", type=int, default=1, help="并发进程数(每进程注册 --count 个)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--dob", default="1996-05-20")
    ap.add_argument("--mail-timeout", type=float, default=180.0, help="等码秒数")
    ap.add_argument("--tries", type=int, default=1,
                    help="单账号尝试次数(默认1=失败即换下一个邮箱)")
    ap.add_argument("--max-mail", type=int, default=0, help="每 worker 最多邮箱数(默认 count*2)")
    ap.add_argument("--interval", type=float, default=0.0)
    ap.add_argument("--base-port", type=int, default=9236)
    ap.add_argument("--out", default=str(BASE_DIR / "accounts.jsonl"))
    ap.add_argument("--no-key", action="store_true", help="跳过领 EdgeOne API Key")
    ap.add_argument("--upstream", default="", help="上游上传地址 http://host:port(留空读 UPSTREAM_API)")
    args = ap.parse_args()
    workers = max(1, args.workers)

    # CLIP daemon 由父进程统一管理
    daemon_proc = None
    if not _daemon_up(3):
        print("[model] 拉起 CLIP daemon 并预加载模型(首次约 1~2 分钟)…", flush=True)
        daemon_proc = subprocess.Popen(
            [sys.executable, str(BASE_DIR / "clip_daemon.py")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if _daemon_up(240):
            print("[model] ✔ 模型已预加载", flush=True)
        else:
            print("[model] ⚠ daemon 未就绪, 退回每题子进程加载", flush=True)
            daemon_proc = None
    else:
        print("[model] CLIP daemon 已在运行(8701), 直接复用", flush=True)

    total_target = args.count * workers
    print(f"[info] 进程数 {workers} x 每进程 {args.count} = 目标 {total_target} 个", flush=True)
    for w in range(workers):
        for f in (f"accounts_w{w}.jsonl", f"key_w{w}.txt"):
            try:
                os.remove(str(BASE_DIR / f))
            except OSError:
                pass

    import multiprocessing
    ctx = multiprocessing.get_context("spawn")
    procs = []
    for w in range(workers):
        p = ctx.Process(target=_worker_proc,
                        args=(w, args.count, args.tries, args.headless, args.dob,
                              args.mail_timeout, args.base_port, args.max_mail,
                              not args.no_key, args.upstream or None))
        p.start()
        procs.append(p)
    for p in procs:
        p.join()

    if daemon_proc is not None:
        try:
            daemon_proc.terminate()
        except Exception:
            pass

    ok_total = fail_total = 0
    with open(args.out, "a", encoding="utf-8") as fout:
        for w in range(workers):
            acc = BASE_DIR / f"accounts_w{w}.jsonl"
            if acc.exists():
                content = acc.read_text(encoding="utf-8")
                fout.write(content)
                for line in content.splitlines():
                    try:
                        if json.loads(line).get("status") == "ok":
                            ok_total += 1
                        else:
                            fail_total += 1
                    except Exception:
                        pass
    if not args.no_key:
        with open(str(BASE_DIR / "key.txt"), "a", encoding="utf-8") as fk:
            for w in range(workers):
                kp = BASE_DIR / f"key_w{w}.txt"
                if kp.exists():
                    for line in kp.read_text(encoding="utf-8").splitlines():
                        if line.strip():
                            fk.write(line.strip() + "\n")
    print(f"\n===== 完成: 成功 {ok_total}/{total_target} (失败跳过 {fail_total}) =====", flush=True)
    print(f"明细已追加到: {args.out}", flush=True)
    return 0 if ok_total >= total_target else 1


if __name__ == "__main__":
    raise SystemExit(main())
