"""
カービィカフェ東京 予約空き状況チェッカー
- 4名で予約可能な日時を毎日チェックしてテキストファイルに出力する
- 毎月10日18:00に翌月分が解禁される仕様に対応
- サイトが表示している月を基点に、前月・翌月も必要に応じて確認する
"""

import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, time as dtime
from PIL import Image
import jpholiday
from playwright.async_api import async_playwright

sys.stdout.reconfigure(encoding="utf-8")

# スケジュールタスク（SYSTEMアカウント）でも正しいブラウザパスを参照させる
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    r"C:\Users\mitsu\AppData\Local\ms-playwright",
)

URL = "https://kirbycafe-reserve.com/guest/tokyo/reserve/"
PARTY_SIZE = 4
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── LINE 設定 ──────────────────────────────────────────────
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
# ────────────────────────────────────────────────────────────


def is_online(timeout: int = 5) -> bool:
    """インターネット接続を確認する。"""
    try:
        urllib.request.urlopen("https://www.google.com", timeout=timeout)
        return True
    except Exception:
        return False


def wait_for_network(max_retries: int = 3, interval_sec: int = 120) -> bool:
    """ネット接続が回復するまで待機する。接続できたらTrue、タイムアウトしたらFalse。"""
    for attempt in range(1, max_retries + 1):
        if is_online():
            return True
        print(f"[ネットワーク未接続] {attempt}/{max_retries} 回目 — {interval_sec}秒後に再試行します...")
        time.sleep(interval_sec)
    return False


def is_notifiable_slot(slot: str, year: int) -> bool:
    """平日18:30以降 または 土日祝 の枠ならTrue。"M/D(曜)HH:MM" 形式を想定。"""
    m = re.match(r'(\d+)/(\d+)[（(]([月火水木金土日])[）)](\d+):(\d+)', slot)
    if not m:
        return False
    month, day, dow = int(m.group(1)), int(m.group(2)), m.group(3)
    slot_time = dtime(int(m.group(4)), int(m.group(5)))
    slot_date = date(year, month, day)
    if dow in ('土', '日') or jpholiday.is_holiday(slot_date):
        return True
    return slot_time >= dtime(18, 30)


def send_line_notification(message: str) -> bool:
    """ボットの友だち全員にブロードキャスト送信する。"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        return False
    payload = json.dumps({
        "messages": [{"type": "text", "text": message}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/broadcast",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status == 200
    except urllib.error.HTTPError as e:
        print(f"[LINE通知エラー] {e.code}: {e.read().decode()}")
        return False

JS_GET_AVAILABILITY = r"""
    () => {
        const available = [];

        // 年月をカレンダーヘッダーから取得
        const allEls = Array.from(document.querySelectorAll('*'));
        const monthEl = allEls.find(el =>
            /\d{4}年\d{1,2}月/.test((el.textContent || '').trim()) && el.children.length === 0
        );
        const monthMatch = monthEl ? (monthEl.textContent || '').match(/(\d{4})年(\d{1,2})月/) : null;
        const year  = monthMatch ? parseInt(monthMatch[1]) : null;
        const month = monthMatch ? parseInt(monthMatch[2]) : null;

        const table = document.querySelector('table');
        if (!table || !month) return { available, year, month };

        const rows = Array.from(table.querySelectorAll('tr'));
        if (rows.length < 2) return { available, year, month };

        // 列ヘッダーから「列インデックス → M/D(曜)」マップを作成
        const dateMap = {};
        Array.from(rows[0].querySelectorAll('th, td')).forEach((cell, i) => {
            const t = (cell.textContent || '').replace(/\s+/g, '');
            const m = t.match(/(\d+)[（(]([月火水木金土日])[）)]/);
            if (m) dateMap[i] = `${month}/${parseInt(m[1])}(${m[2]})`;
        });

        // データ行を走査して空きセルを収集
        rows.slice(1).forEach(row => {
            const cells = Array.from(row.querySelectorAll('th, td'));
            if (!cells.length) return;
            const timeText = (cells[0].textContent || '').trim();
            if (!/^\d{2}:\d{2}$/.test(timeText)) return;  // 時間行以外はスキップ

            cells.forEach((cell, i) => {
                if (i === 0 || !dateMap[i]) return;
                const text = (cell.textContent || '').trim();
                const cls  = cell.className || '';
                const isDisabled = cls.includes('disable') || cls.includes('close') || cls.includes('gray');
                const isFull     = text === '×' || text === 'x' || text === 'X';
                if (!isFull && !isDisabled && text.length > 0) {
                    available.push(`${dateMap[i]}${timeText}`);
                }
            });
        });

        return { available, year, month };
    }
"""

def js_click_month_nav(month_num):
    """指定した月番号を含むナビボタンをクリックするJS（前月・翌月共用）"""
    return f"""
    () => {{
        const search = '{month_num}月';
        // 全要素からtextContentで月番号を含む短い要素を探す
        const candidates = Array.from(document.querySelectorAll('*'))
            .filter(el => {{
                const t = (el.textContent || '').trim();
                return t.includes(search) && t.length < 20;
            }});
        const best = candidates.sort((a, b) => a.textContent.length - b.textContent.length)[0];
        if (best) {{
            // 要素自体 + 最近の祖先ボタン/リンクの両方にclickを送る
            best.dispatchEvent(new MouseEvent('click', {{bubbles: true, cancelable: true}}));
            const parent = best.closest('button, a, [role="button"]');
            if (parent && parent !== best) {{
                parent.dispatchEvent(new MouseEvent('click', {{bubbles: true, cancelable: true}}));
            }}
            return best.textContent.trim();
        }}
        return null;
    }}
"""


def js_debug_nav_elements(month_num):
    """月ナビ周辺の要素をデバッグ出力するJS"""
    return f"""
    () => {{
        const search = '{month_num}月';
        const all = Array.from(document.querySelectorAll('*'));
        return {{
            byTextContent: all
                .filter(el => (el.textContent || '').trim().includes(search) && (el.textContent || '').trim().length < 30)
                .map(el => ({{
                    tag: el.tagName,
                    text: (el.textContent || '').trim().substring(0, 30),
                    cls: (el.className || '').substring(0, 60),
                    role: el.getAttribute('role') || '',
                    id: el.id || '',
                }})),
            allButtons: all
                .filter(el => el.tagName === 'BUTTON' || (el.getAttribute('role') || '').toLowerCase() === 'button')
                .map(el => ({{
                    tag: el.tagName,
                    text: (el.textContent || '').trim().substring(0, 40),
                    cls: (el.className || '').substring(0, 60),
                }})),
        }};
    }}
"""


def cleanup_old_files(today_str: str) -> list[str]:
    """今日より前の日付の kirby cafe 関連ファイルを削除する。削除したファイル名のリストを返す。"""
    deleted = []
    for filename in os.listdir(OUTPUT_DIR):
        if len(filename) < 10:
            continue
        file_date = filename[:10]  # "YYYY-MM-DD"
        if file_date >= today_str:
            continue
        if "_kirby_" in filename:
            filepath = os.path.join(OUTPUT_DIR, filename)
            try:
                os.remove(filepath)
                deleted.append(filename)
            except OSError:
                pass
    return deleted


def combine_screenshots(img_paths, output_path):
    """複数のスクリーンショットを縦に結合して1枚の画像として保存し、元ファイルを削除する"""
    existing = [p for p in img_paths if os.path.exists(p)]
    if len(existing) < 2:
        return None
    images = [Image.open(p) for p in existing]
    max_width = max(img.width for img in images)
    total_height = sum(img.height for img in images)
    combined = Image.new("RGB", (max_width, total_height), (255, 255, 255))
    y = 0
    for img in images:
        combined.paste(img, (0, y))
        y += img.height
    combined.save(output_path)
    # 結合元の個別ファイルを削除
    for img, path in zip(images, existing):
        img.close()
    for path in existing:
        os.remove(path)
    return output_path


async def try_click_month(page, month_num, results, silent=False):
    """月ナビボタンをクリック。Playwright ネイティブ → JS の順で試みる。"""
    target = f"{month_num}月"

    # 方法1: Playwright ネイティブ（button / a でテキスト一致）
    for selector in [f"button:has-text('{target}')", f"a:has-text('{target}')"]:
        loc = page.locator(selector)
        count = await loc.count()
        for i in range(count):
            try:
                txt = (await loc.nth(i).inner_text()).strip()
                if len(txt) < 20:
                    await loc.nth(i).click(timeout=3000)
                    await page.wait_for_timeout(1500)
                    return txt
            except Exception:
                continue

    # 方法2: JS（全要素を textContent で検索）
    clicked = await page.evaluate(js_click_month_nav(month_num))
    if clicked:
        await page.wait_for_timeout(1500)
        return clicked

    # デバッグ情報を出力（silent=False のときのみ）
    if not silent:
        debug = await page.evaluate(js_debug_nav_elements(month_num))
        results.append(f"  [debug] {target}関連要素: {debug.get('byTextContent', [])}")
        results.append(f"  [debug] 全ボタン: {debug.get('allButtons', [])}")

    return None


async def check_calendar(page, label, results, date_str):
    """現在表示中のカレンダーの空き状況を取得して結果に追記する"""
    await page.wait_for_timeout(2000)
    data = await page.evaluate(JS_GET_AVAILABILITY)
    year = data.get("year")
    month = data.get("month")
    month_label = f"{year}年{month}月" if year and month else label
    slots = data.get("available", [])

    # スクロール可能な内部コンテナのoverflow制限を解除してから撮影
    await page.evaluate("""
        () => {
            document.querySelectorAll('*').forEach(el => {
                const s = window.getComputedStyle(el);
                if (s.overflow === 'auto' || s.overflow === 'scroll' ||
                    s.overflowY === 'auto' || s.overflowY === 'scroll') {
                    el.style.overflow = 'visible';
                    el.style.overflowY = 'visible';
                    el.style.height = 'auto';
                    el.style.maxHeight = 'none';
                }
            });
        }
    """)
    await page.wait_for_timeout(300)
    screenshot_path = os.path.join(OUTPUT_DIR, f"{date_str}_kirby_{label}.png")
    await page.screenshot(path=screenshot_path, full_page=True)

    if slots:
        results.append(f"【空きあり】{month_label}: {len(slots)} 件")
        results.append("  " + ", ".join(slots[:30]))
    else:
        results.append(f"【満席】{month_label}: 予約可能な日程なし")
    results.append(f"  スクリーンショット: {screenshot_path}")

    return {"year": year, "month": month, "screenshot": screenshot_path, "slots": slots}


async def check_availability():
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d_%H-%M")   # 例: 2026-04-26_20-15
    today_str = now.strftime("%Y-%m-%d")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    output_file = os.path.join(OUTPUT_DIR, f"{date_str}_kirby_cafe_availability.txt")

    # ネット接続確認（未接続なら最大3回・2分間隔でリトライ）
    if not wait_for_network(max_retries=3, interval_sec=120):
        print(f"[{now_str}] ネットワーク未接続のためスキップします")
        return

    # 前日以前のファイルを削除（新しい日の最初の稼働で実行される）
    deleted = cleanup_old_files(today_str)

    results = [
        f"=== カービィカフェ東京 空き状況チェック ({now_str}) ===",
        f"対象: {PARTY_SIZE}名 / URL: {URL}",
        "",
    ]
    if deleted:
        results.append(f"前日分ファイル削除: {', '.join(deleted)}")
        results.append("")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="ja-JP",
        )
        page = await context.new_page()

        try:
            await page.goto(URL, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            # 冒頭モーダルの「OK」を閉じる
            ok_btn = page.locator("button:has-text('OK'), input[value='OK']")
            if await ok_btn.count() > 0:
                await ok_btn.first.click()
                await page.wait_for_timeout(2000)
                results.append("✓ 確認ダイアログを閉じました")

            # 人数選択
            await page.wait_for_timeout(2000)
            selected = None

            # 方法1: ネイティブ <select> 要素
            try:
                sel_el = page.locator("select").first
                if await sel_el.count() > 0:
                    opts = await sel_el.evaluate(
                        "el => Array.from(el.options).map(o => ({value: o.value, text: o.text}))"
                    )
                    target = next(
                        (o for o in opts if f"{PARTY_SIZE}名" in o["text"] or o["value"] == str(PARTY_SIZE)),
                        None,
                    )
                    if target:
                        await sel_el.select_option(value=target["value"])
                        selected = target["text"].strip()
                        await page.wait_for_timeout(1000)
            except Exception:
                pass

            # 方法2: カスタムドロップダウン（クリック → テキスト選択）
            if not selected:
                for selector in ["[class*='select']", "[role='combobox']", "[role='listbox']", "div[tabindex]", "[class*='dropdown']"]:
                    try:
                        loc = page.locator(selector).first
                        if await loc.count() > 0:
                            await loc.click(timeout=3000)
                            await page.wait_for_timeout(1500)
                            for label in [f"{PARTY_SIZE}名様", f"{PARTY_SIZE}名", f"{PARTY_SIZE}人"]:
                                opt = page.get_by_text(label, exact=True)
                                if await opt.count() > 0:
                                    await opt.first.click()
                                    selected = label
                                    break
                            if selected:
                                break
                    except Exception:
                        continue

            # 方法3: JS で value をセットして change/input イベントを発火
            if not selected:
                js_result = await page.evaluate(f"""
                    () => {{
                        const sel = document.querySelector('select');
                        if (!sel) return null;
                        const opts = Array.from(sel.options);
                        const target = opts.find(o => o.text.includes('{PARTY_SIZE}名') || o.value === '{PARTY_SIZE}');
                        if (!target) return null;
                        sel.value = target.value;
                        ['input', 'change'].forEach(evt =>
                            sel.dispatchEvent(new Event(evt, {{bubbles: true}}))
                        );
                        return target.text;
                    }}
                """)
                if js_result:
                    selected = js_result.strip()
                    await page.wait_for_timeout(1000)

            if selected:
                results.append(f"✓ {selected}を選択しました")
            else:
                results.append(f"△ {PARTY_SIZE}名の選択に失敗しました")

            await page.wait_for_timeout(3000)

            # 結合用スクリーンショット収集リスト（古い月順）
            ordered_screenshots = []
            available_months = []  # LINE通知用の空き情報

            # ── STEP 1: サイトが表示しているデフォルト月を確認 ──
            results.append("")
            site_info = await check_calendar(page, "site_default", results, date_str)
            site_year = site_info["year"]
            site_month = site_info["month"]
            site_screenshot = site_info["screenshot"]
            if site_info["slots"]:
                available_months.append((f"{site_year}年{site_month}月", site_info["slots"]))

            # ── STEP 2: 前月（実際の今月）を確認 ──
            # サイトが「翌月」を表示している場合、前月 = 実際の今月（まだ日程が残っている）
            prev_month = site_month - 1 if site_month and site_month > 1 else 12
            prev_year = site_year if site_month and site_month > 1 else (site_year - 1 if site_year else None)

            if prev_year == now.year and prev_month == now.month:
                prev_clicked = await try_click_month(page, prev_month, results)
                if prev_clicked:
                    results.append("")
                    results.append(f"--- {prev_year}年{prev_month}月（今月） ---")
                    prev_info = await check_calendar(page, "prev_month", results, date_str)
                    ordered_screenshots.append(prev_info["screenshot"])  # 当月を先頭に
                    if prev_info["slots"]:
                        available_months.insert(0, (f"{prev_year}年{prev_month}月", prev_info["slots"]))
                    # デフォルト月に戻る
                    await try_click_month(page, site_month, results, silent=True)
                    await page.wait_for_timeout(1500)
                else:
                    results.append(f"\n△ 前月（{prev_year}年{prev_month}月）ボタンが見つかりませんでした")

            ordered_screenshots.append(site_screenshot)  # サイトデフォルト月

            # ── STEP 3: 翌月を確認（サイト表示月の10日18:00以降に解禁） ──
            if site_year and site_month:
                site_month_10th = datetime(site_year, site_month, 10, 18, 0)
                if now >= site_month_10th:
                    next_month = site_month + 1 if site_month < 12 else 1
                    next_year = site_year if site_month < 12 else site_year + 1
                    next_clicked = await try_click_month(page, next_month, results, silent=True)
                    if next_clicked:
                        results.append("")
                        results.append(f"--- {next_year}年{next_month}月（翌月・解禁済み） ---")
                        next_info = await check_calendar(page, "next_month", results, date_str)
                        ordered_screenshots.append(next_info["screenshot"])
                        if next_info["slots"]:
                            available_months.append((f"{next_year}年{next_month}月", next_info["slots"]))
                    else:
                        results.append(f"\n△ 翌月（{next_month}月）ボタンが見つかりませんでした")

            # ── 結合画像を出力 ──
            if len(ordered_screenshots) >= 2:
                combined_path = os.path.join(OUTPUT_DIR, f"{date_str}_kirby_cafe_combined.png")
                combine_screenshots(ordered_screenshots, combined_path)
                results.append("")
                results.append(f"結合スクリーンショット: {combined_path}")

            # ── LINE 通知（平日18:30以降 or 土日祝 の空きがある場合のみ） ──
            notify_lines = []
            for month_label, slots in available_months:
                filtered = [s for s in slots if is_notifiable_slot(s, now.year)]
                if filtered:
                    notify_lines.append(f"\n【{month_label}】{len(filtered)}件")
                    notify_lines.append(", ".join(filtered[:20]))
            if notify_lines:
                msg = f"🎉 カービィカフェ東京 {PARTY_SIZE}名 空きあり！" + "".join(notify_lines) + f"\n\n{URL}"
                notified = send_line_notification(msg)
                results.append(f"LINE通知: {'送信済み' if notified else '未設定のためスキップ'}")
            else:
                results.append("LINE通知: 対象外の時間帯のみのため送信なし")

        except Exception as e:
            results.append(f"[エラー] {e}")

        finally:
            await browser.close()

    results.append("")

    with open(output_file, "w", encoding="utf-8") as f:
        f.write("\n".join(results) + "\n")

    print("\n".join(results))


if __name__ == "__main__":
    asyncio.run(check_availability())
