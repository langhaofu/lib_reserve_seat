#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════╗
║   成都信息工程大学图书馆 · 自动抢座脚本 v2.0            ║
║   完整流程: EasyConnect VPN → SSO → 图书馆座位预约       ║
╚══════════════════════════════════════════════════════════╝

【依赖安装】
pip install playwright opencv-python-headless numpy pycryptodome ddddocr
python -m playwright install chromium

【使用说明】
直接运行: python reserve.py
脚本会自动打开浏览器，自动完成登录和抢座，全程无需人工干预。
"""

import asyncio
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

# ─── 依赖检查 ─────────────────────────────────────────────
missing = []
try:
    import cv2
    import numpy as np
except ImportError:
    missing.append("opencv-python-headless numpy")

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
except ImportError:
    missing.append("pycryptodome")

try:
    from playwright.async_api import async_playwright, Page, BrowserContext
except ImportError:
    missing.append("playwright")

try:
    import ddddocr
    _ocr = ddddocr.DdddOcr(show_ad=False)
    HAS_OCR = True
except ImportError:
    HAS_OCR = False
    print("[警告] ddddocr 未安装，图片验证码将需要手动输入")
    print("       pip install ddddocr  可启用自动识别")

if missing:
    print(f"[错误] 缺少以下依赖，请安装: pip install {' '.join(missing)}")
    sys.exit(1)

# ═══════════════════════════════════════════════════════════
#   ⚙️  配 置 区（仅需修改此处）
# ═══════════════════════════════════════════════════════════

# 读取本地 .env（不提交到 Git），格式见 .env.example
def _load_env(path: str = ".env") -> None:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value

_load_env()

# 账号密码（EasyConnect VPN 和 SSO 统一用同一套），从环境变量读取
USERNAME = os.environ.get("RESERVE_USERNAME", "")
PASSWORD = os.environ.get("RESERVE_PASSWORD", "")

# 目标房间（优先级排序）：38 = 航空港四楼F区
PREFERRED_ROOM_IDS: List[int] = [38]

# 预约日期：None = 自动选明天
TARGET_DATE: Optional[str] = None

# 轮询间隔（秒），建议 0.5~2
POLL_INTERVAL = 0.8

# 超过几点停止抢座
STOP_HOUR = 23

# ── URL 常量 ────────────────────────────────────────────────
# EasyConnect WebVPN 门户
WEBVPN_URL      = "https://webvpn.cuit.edu.cn/portal/?redirect_uri=http%3A%2F%2Fywtb-cuit-edu-cn-s.webvpn.cuit.edu.cn%3A8118%2F"
# 经 WebVPN 访问的座位预约系统
YWTB_BASE       = "http://ywtb-cuit-edu-cn-s.webvpn.cuit.edu.cn:8118"
YWTB_LOGIN      = f"{YWTB_BASE}/#/login"
YWTB_HOME       = f"{YWTB_BASE}/file/apps/yypt/index.html#/ic/home"
# API 根地址（通过 WebVPN）
API_BASE        = f"{YWTB_BASE}/third_api/yypt/ic-web"
# SSO 统一认证（带图片验证码）
SSO_LOGIN_RE    = re.compile(r"sso-cuit|authserver/login", re.I)
# EasyConnect 登录判断
EASYCONN_RE     = re.compile(r"webvpn\.cuit\.edu\.cn/portal", re.I)

# ═══════════════════════════════════════════════════════════
#   工 具
# ═══════════════════════════════════════════════════════════

def log(msg: str, level: str = "INFO") -> None:
    icons = {"INFO": "  ", "OK": "✅", "WARN": "⚠ ", "ERR": "❌",
             "WAIT": "⏳", "FOUND": "🟢", "CAP": "🔐"}
    t = datetime.now().strftime("%H:%M:%S")
    print(f"[{t}] {icons.get(level,'  ')} {msg}", flush=True)


def target_date() -> str:
    if TARGET_DATE:
        return TARGET_DATE
    return (datetime.now() + timedelta(days=1)).strftime("%Y%m%d")


# ═══════════════════════════════════════════════════════════
#   图 片 验 证 码 识 别
# ═══════════════════════════════════════════════════════════

def ocr_image(img_bytes: bytes) -> str:
    """用 ddddocr 识别图片验证码，失败则返回空字符串"""
    if HAS_OCR:
        try:
            result = _ocr.classification(img_bytes)
            log(f"OCR 识别结果: {result}", "CAP")
            return result.strip()
        except Exception as e:
            log(f"OCR 失败: {e}", "WARN")
    return ""


async def get_captcha_img_bytes(page: Page, img_selector: str) -> Optional[bytes]:
    """截取页面上验证码图片的字节数据"""
    try:
        loc = page.locator(img_selector)
        if await loc.count():
            return await loc.first.screenshot()
        # 尝试通过 src 属性获取
        src = await page.locator(img_selector).first.get_attribute("src")
        if src and src.startswith("data:image"):
            b64 = src.split(",", 1)[1]
            return base64.b64decode(b64)
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════
#   登 录 流 程
# ═══════════════════════════════════════════════════════════

async def login_easyconnect(page: Page) -> bool:
    """登录 EasyConnect WebVPN（图1）"""
    log("EasyConnect 登录页...")
    try:
        # 用户名
        for sel in ['#username', 'input[name="username"]', 'input[placeholder*="用户"]',
                    'input[type="text"]']:
            loc = page.locator(sel)
            if await loc.count():
                await loc.first.fill(USERNAME)
                break

        # 密码
        for sel in ['#password', 'input[name="password"]', 'input[type="password"]']:
            loc = page.locator(sel)
            if await loc.count():
                await loc.first.fill(PASSWORD)
                break

        # 勾选协议（如果有）
        chk_sel = 'input[type="checkbox"]'
        chk = page.locator(chk_sel)
        if await chk.count() and not await chk.first.is_checked():
            await chk.first.click()

        # 点击登录
        for sel in ['#login-submit', 'button[type="submit"]', '#loginButton',
                    'button:has-text("登录")', 'input[value="登录"]']:
            loc = page.locator(sel)
            if await loc.count():
                await loc.first.click()
                break

        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        await page.wait_for_timeout(2000)
        log(f"EasyConnect 登录后: {page.url[:60]}", "OK")
        return True
    except Exception as e:
        log(f"EasyConnect 登录异常: {e}", "ERR")
        return False


async def login_sso_with_captcha(page: Page, max_retry: int = 5) -> bool:
    """
    登录 SSO 统一身份认证（图4），含图片验证码。
    自动识别验证码，最多重试 max_retry 次。
    """
    log("SSO 统一认证登录，识别图片验证码...", "CAP")

    for attempt in range(1, max_retry + 1):
        try:
            # 填用户名
            for sel in ['input[placeholder*="账户"]', 'input[placeholder*="学号"]',
                        '#username', 'input[name="username"]']:
                loc = page.locator(sel)
                if await loc.count():
                    await loc.first.click()
                    await loc.first.fill("")
                    await loc.first.fill(USERNAME)
                    break

            # 填密码
            for sel in ['#password', 'input[name="password"]', 'input[type="password"]']:
                loc = page.locator(sel)
                if await loc.count():
                    await loc.first.fill(PASSWORD)
                    break

            # 找验证码图片并识别
            cap_code = ""
            cap_input_sel = ""
            for img_sel in ['img[src*="captcha"]', 'img[src*="code"]',
                            '#captchaImg', '.captcha-img', 'img[id*="captcha"]',
                            'img[id*="verifyCode"]', 'img.capimg']:
                img_loc = page.locator(img_sel)
                if await img_loc.count():
                    img_bytes = await img_loc.first.screenshot()
                    cap_code = ocr_image(img_bytes)
                    if not cap_code and not HAS_OCR:
                        cap_code = input(f"[手动] 请输入验证码 (第{attempt}次): ").strip()
                    break

            # 找验证码输入框
            for inp_sel in ['input[placeholder*="验证码"]', '#captcha', '#verifyCode',
                            'input[name="captcha"]', 'input[id*="captcha"]',
                            'input[id*="code"]']:
                loc = page.locator(inp_sel)
                if await loc.count():
                    cap_input_sel = inp_sel
                    await loc.first.fill(cap_code)
                    break

            await page.wait_for_timeout(300)

            # 点击登录
            for sel in ['button[type="submit"]', '#login-submit', '#loginBtn',
                        'button:has-text("登 录")', 'button:has-text("登录")',
                        'input[type="submit"]']:
                loc = page.locator(sel)
                if await loc.count():
                    await loc.first.click()
                    break

            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            await page.wait_for_timeout(2000)

            cur = page.url
            log(f"SSO 提交后: {cur[:70]}")

            # 如果还在登录页 → 验证码错了，刷新重试
            if SSO_LOGIN_RE.search(cur):
                log(f"验证码错误，刷新重试（{attempt}/{max_retry}）...", "WARN")
                # 点击验证码图片刷新
                for img_sel in ['img[src*="captcha"]', 'img[src*="code"]', '.capimg']:
                    if await page.locator(img_sel).count():
                        await page.locator(img_sel).first.click()
                        await page.wait_for_timeout(800)
                        break
                continue
            else:
                log("SSO 登录成功 ✅", "OK")
                return True

        except Exception as e:
            log(f"SSO 登录第{attempt}次异常: {e}", "WARN")
            await page.wait_for_timeout(1000)

    log("SSO 登录失败，已超出最大重试次数", "ERR")
    return False


async def do_full_login(page: Page) -> bool:
    """
    完整登录流程：
    1. EasyConnect VPN
    2. WebVPN 座位系统 → SSO 认证
    """
    log("=" * 50)
    log("开始完整登录流程...")

    # Step 1: 打开 EasyConnect 登录
    await page.goto(WEBVPN_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(2000)
    cur = page.url

    if EASYCONN_RE.search(cur) or "portal" in cur.lower():
        # 检查是否已经登录了 EasyConnect
        # 如果出现了登录表单
        has_login_form = await page.locator('input[type="password"]').count()
        if has_login_form:
            ok = await login_easyconnect(page)
            if not ok:
                return False
        else:
            log("EasyConnect 已登录", "OK")

    # Step 2: 访问 WebVPN 下的座位系统
    await page.wait_for_timeout(1000)
    log(f"导航到座位系统: {YWTB_LOGIN}")
    await page.goto(YWTB_LOGIN, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)
    cur = page.url
    log(f"当前页面: {cur[:70]}")

    # Step 3: 判断是否需要 SSO 登录
    if SSO_LOGIN_RE.search(cur):
        ok = await login_sso_with_captcha(page)
        if not ok:
            return False
        await page.wait_for_timeout(2000)
        cur = page.url
        log(f"认证后页面: {cur[:70]}")

    # Step 4: 导航到图书馆主系统
    if "ywtb" not in cur.lower() and "ic" not in cur.lower():
        log("导航到座位预约主页...")
        await page.goto(YWTB_HOME, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

    log("登录流程完成 ✅", "OK")
    log(f"当前: {page.url[:60]}")
    return True


# ═══════════════════════════════════════════════════════════
#   滑 块 验 证 码（blockPuzzle）
# ═══════════════════════════════════════════════════════════

def find_slide_distance(bg_b64: str, piece_b64: str) -> int:
    def decode(b64_str: str):
        raw = base64.b64decode(b64_str.split(",")[-1])
        return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)

    bg = decode(bg_b64)
    piece = decode(piece_b64)
    if bg is None or piece is None:
        return 120

    def to_gray(img):
        if len(img.shape) == 3:
            if img.shape[2] == 4:
                alpha = img[:, :, 3]
                g = cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2GRAY)
                return cv2.bitwise_and(g, g, mask=alpha)
            return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return img

    bg_edge    = cv2.Canny(cv2.GaussianBlur(to_gray(bg),    (3,3), 0), 100, 200)
    piece_edge = cv2.Canny(cv2.GaussianBlur(to_gray(piece), (3,3), 0), 100, 200)
    res = cv2.matchTemplate(bg_edge, piece_edge, cv2.TM_CCOEFF_NORMED)
    _, _, _, loc = cv2.minMaxLoc(res)
    return int(loc[0])


def aes_encrypt(key_b64: str, text: str) -> str:
    key = base64.b64decode(key_b64)
    iv  = key[:16]
    return base64.b64encode(
        AES.new(key, AES.MODE_CBC, iv).encrypt(pad(text.encode(), 16))
    ).decode()


async def solve_block_captcha(page: Page, token: str) -> Optional[dict]:
    """获取 + 解题 blockPuzzle 验证码，返回可用于预约 POST 的 captcha 对象"""
    try:
        resp = await page.evaluate(
            """async (base, tok) => {
                const r = await fetch(base + '/captcha/get', {
                    method: 'POST', credentials: 'include',
                    headers: {'Content-Type':'application/json','token':tok,'lan':'1'},
                    body: JSON.stringify({captchaType:'blockPuzzle',clientUid:null,ts:Date.now()})
                });
                return await r.json();
            }""",
            API_BASE, token
        )
        if not resp or not resp.get("data"):
            log(f"验证码获取失败: {resp}", "WARN")
            return None

        d   = resp["data"]
        x   = find_slide_distance(d.get("originalImageBase64",""), d.get("jigsawImageBase64",""))
        tok = d.get("token","")
        key = d.get("secretKey","")

        for delta in [0, 5, -5, 10, -10, 15, -15, 20, -20, 25, -25]:
            xv  = max(0, x + delta)
            enc = aes_encrypt(key, json.dumps({"x": xv, "y": 5}))
            chk = await page.evaluate(
                """async (base, tok, body) => {
                    const r = await fetch(base+'/captcha/check',{
                        method:'POST',credentials:'include',
                        headers:{'Content-Type':'application/json','token':tok,'lan':'1'},
                        body:JSON.stringify(body)
                    });
                    return await r.json();
                }""",
                API_BASE, token,
                {"captchaType":"blockPuzzle","token":tok,"pointJson":enc}
            )
            if chk and (chk.get("success") or str(chk.get("code","")) in ("0","200")):
                log(f"滑块验证码通过 x={xv}", "OK")
                return {"captchaType":"blockPuzzle","token":tok,"pointJson":enc}

        log("滑块验证码全部偏移均失败", "WARN")
    except Exception as e:
        log(f"滑块验证码异常: {e}", "WARN")
    return None


# ═══════════════════════════════════════════════════════════
#   座 位 查 询 & 预 约
# ═══════════════════════════════════════════════════════════

async def get_token(page: Page) -> str:
    try:
        r = await page.evaluate(
            """async (base) => {
                try {
                    const r = await fetch(base+'/auth/userInfo',{credentials:'include'});
                    const d = await r.json();
                    return d?.data?.token ?? '';
                } catch(e){return '';}
            }""", API_BASE
        )
        return r or ""
    except Exception:
        return ""


async def fetch_free_seats(page: Page, room_id: int, date_str: str, token: str) -> List[dict]:
    """查询指定房间指定日期的空闲座位"""
    try:
        raw = await page.evaluate(
            """async (base, roomId, date, tok) => {
                const url = base+'/reserve?roomIds='+roomId+'&resvDates='+date+'&sysKind=8';
                const r = await fetch(url, {
                    credentials:'include',
                    headers:{'token':tok,'lan':'1'}
                });
                return await r.json();
            }""",
            API_BASE, room_id, date_str, token
        )
        if not raw:
            return []

        data = raw.get("data", [])
        items: List[dict] = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    items.extend(v)

        # devStatus == 0 → 空闲
        free = [it for it in items if it.get("devStatus", -1) == 0]
        return free
    except Exception as e:
        log(f"查询座位失败: {e}", "WARN")
        return []


async def get_all_room_ids(page: Page, token: str) -> List[int]:
    try:
        raw = await page.evaluate(
            """async (base, tok) => {
                const r = await fetch(base+'/roomDevice/roomInfos?sysKind=8', {
                    credentials:'include',
                    headers:{'token':tok,'lan':'1'}
                });
                return await r.json();
            }""", API_BASE, token
        )
        data = raw.get("data", []) if raw else []
        ids = []
        if isinstance(data, list):
            for room in data:
                rid = room.get("roomId") or room.get("id")
                if rid:
                    ids.append(int(rid))
        return ids
    except Exception:
        return []


async def reserve_seat(page: Page, seat: Dict[str, Any], date_str: str, token: str) -> bool:
    """提交预约请求"""
    dev_id   = seat.get("devId") or seat.get("id") or seat.get("devNo")
    dev_name = seat.get("devName", str(dev_id))
    if not dev_id:
        return False

    log(f"尝试预约: {dev_name}（devId={dev_id}）")

    captcha = await solve_block_captcha(page, token)
    if not captcha:
        log("验证码解题失败，跳过此座位", "WARN")
        return False

    y, m, d = date_str[0:4], date_str[4:6], date_str[6:8]
    begin   = f"{y}-{m}-{d} 08:00:00"
    end     = f"{y}-{m}-{d} 22:00:00"

    payload = {
        "sysKind":       8,
        "appAccNo":      dev_id,
        "memberKind":    1,
        "resvMember":    [],
        "resvBeginTime": begin,
        "resvEndTime":   end,
        "testName":      "",
        "resvProperty":  0,
        "resvDates":     [date_str],
        "captcha":       captcha,
    }

    resp = await page.evaluate(
        """async (base, tok, body) => {
            try {
                const r = await fetch(base+'/reserve', {
                    method:'POST', credentials:'include',
                    headers:{'Content-Type':'application/json','token':tok,'lan':'1'},
                    body: JSON.stringify(body)
                });
                return await r.json();
            } catch(e) { return {error:e.toString()}; }
        }""",
        API_BASE, token, payload
    )

    log(f"预约响应: {json.dumps(resp, ensure_ascii=False)[:200]}")

    if resp:
        code = str(resp.get("code", resp.get("status", "")))
        msg  = str(resp.get("message", resp.get("msg", "")))
        if code in ("0","200") or "成功" in msg or resp.get("success"):
            log(f"🎉 预约成功！{dev_name}  {begin} ~ {end}", "OK")
            return True
        elif "已预约" in msg or "already" in msg.lower() or "exist" in msg.lower():
            log(f"该座位被抢，换下一个...", "WARN")
        elif "冲突" in msg or "conflict" in msg.lower():
            log(f"时间冲突（已有其他预约？）: {msg}", "WARN")
        else:
            log(f"预约失败: {msg}", "WARN")
    return False


# ═══════════════════════════════════════════════════════════
#   主 程 序
# ═══════════════════════════════════════════════════════════

async def main():
    date_str = target_date()

    print("\n" + "═" * 60)
    print("  成都信息工程大学图书馆 · 自动抢座脚本 v2.0")
    print("═" * 60)
    log(f"目标日期  : {date_str}")
    log(f"优先房间  : {PREFERRED_ROOM_IDS}")
    log(f"轮询间隔  : {POLL_INTERVAL}s")
    log(f"停止时间  : {STOP_HOUR}:00")
    log(f"验证码OCR : {'ddddocr 自动识别' if HAS_OCR else '手动输入'}")
    print("─" * 60 + "\n")

    async with async_playwright() as pw:
        # 以有头模式启动浏览器（方便观察）
        browser = await pw.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--ignore-certificate-errors",
            ],
        )
        ctx  = await browser.new_context(
            ignore_https_errors=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()

        # ── 完整登录 ──
        success = await do_full_login(page)
        if not success:
            log("登录失败，退出", "ERR")
            input("按 Enter 关闭...")
            await browser.close()
            return

        # ── 确保在预约系统首页 ──
        if "ic" not in page.url and "ywtb" not in page.url:
            await page.goto(YWTB_HOME, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2000)

        # ── 获取 token ──
        token = await get_token(page)
        if token:
            log(f"Auth Token: {token[:20]}...", "OK")
        else:
            log("Token 为空，以 Cookie 模式运行", "WARN")

        # ── 获取全部房间 ──
        all_ids = await get_all_room_ids(page, token)
        room_ids = list(PREFERRED_ROOM_IDS)
        for rid in all_ids:
            if rid not in room_ids:
                room_ids.append(rid)
        log(f"检查房间: {room_ids[:8]}{'…' if len(room_ids)>8 else ''}")

        print("\n" + "═" * 60)
        log("🔍 开始轮询，等待空位出现...", "WAIT")
        print("═" * 60 + "\n")

        round_n = 0
        booked  = False

        while not booked:
            now = datetime.now()
            if now.hour >= STOP_HOUR:
                log(f"已到 {STOP_HOUR}:00，自动退出", "WARN")
                break

            round_n += 1

            # 每 120 轮刷新 token
            if round_n % 120 == 0:
                token = await get_token(page)

            for room_id in room_ids:
                seats = await fetch_free_seats(page, room_id, date_str, token)

                if seats:
                    log(f"🟢 房间 {room_id} 发现 {len(seats)} 个空位！", "FOUND")
                    for seat in seats[:5]:
                        ok = await reserve_seat(page, seat, date_str, token)
                        if ok:
                            booked = True
                            break
                    if booked:
                        break

            if not booked:
                if round_n % 25 == 0:
                    log(f"第 {round_n} 轮，暂无空位...", "WAIT")
                await asyncio.sleep(POLL_INTERVAL)

        # ── 结束 ──
        if booked:
            log("🎊 任务完成！请在浏览器中确认预约记录", "OK")
            await page.screenshot(path="seat_booked.png")
            log("截图已保存 → seat_booked.png")
        else:
            log("脚本退出（未成功预约）")

        input("\n按 Enter 关闭浏览器...")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
