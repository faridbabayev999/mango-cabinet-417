#!/usr/bin/env python3
"""Aachen Ausländerbehörde appointment-slot checker."""

import os
import re
import sys
import json
import time
import pathlib
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BOOKING_URL     = os.environ.get("BOOKING_URL", "https://termine.staedteregion-aachen.de/auslaenderamt/")
AUTHORITY_MATCH = os.environ.get("AUTHORITY_MATCH", "Ausländer")
CONCERN_MATCH   = os.environ.get("CONCERN_MATCH", "")
CONCERN_CATEGORY = os.environ.get("CONCERN_CATEGORY", "Super C")

DISCOVERY = os.environ.get("DISCOVERY", "") not in ("", "0", "false", "False")
HEADLESS  = os.environ.get("HEADLESS", "1") not in ("0", "false", "False")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
ART_DIR    = pathlib.Path(os.environ.get("ARTIFACT_DIR", "artifacts"))
ART_DIR.mkdir(parents=True, exist_ok=True)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
CALLMEBOT_PHONE    = os.environ.get("CALLMEBOT_PHONE", "").strip()
CALLMEBOT_APIKEY   = os.environ.get("CALLMEBOT_APIKEY", "").strip()

NEGATIVE_PATTERNS = [
    r"kein\s+freier?\s+termin",       # "Kein freier Termin verfügbar"
    r"kein\s+termin\s+verf",          # "kein Termin verfügbar"
    r"keine\s+zeiten\s+verf",         # "Keine Zeiten verfügbar" (step heading)
    r"keine\s+freien\s+zeiten",
    r"keine\s+termine\s+(verf|frei|verfügbar)",
    r"leider\s+kein",                 # "ist leider kein Termin verfügbar"
    r"derzeit\s+keine",
    r"aktuell\s+keine",
    r"no\s+appointments?\s+available",
]


def log(*a):
    print(*a, flush=True)


def notify_telegram(text):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID, "text": text,
            "disable_web_page_preview": "false",
        }).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20)
        log("Telegram alert sent.")
    except Exception as e:
        log("Telegram error:", e)


def notify_whatsapp(text):
    if not (CALLMEBOT_PHONE and CALLMEBOT_APIKEY):
        return
    try:
        params = urllib.parse.urlencode({
            "phone": CALLMEBOT_PHONE, "text": text, "apikey": CALLMEBOT_APIKEY,
        })
        urllib.request.urlopen(f"https://api.callmebot.com/whatsapp.php?{params}", timeout=20)
        log("WhatsApp alert sent.")
    except Exception as e:
        log("WhatsApp error:", e)


def notify(text):
    log("NOTIFY:", text)
    notify_telegram(text)
    notify_whatsapp(text)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log("State save error:", e)


def dump_page(page, tag):
    try:
        page.screenshot(path=str(ART_DIR / f"{tag}.png"), full_page=True)
    except Exception as e:
        log("screenshot error:", e)
    try:
        (ART_DIR / f"{tag}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    try:
        txt = page.inner_text("body")
        (ART_DIR / f"{tag}.txt").write_text(txt, encoding="utf-8")
        return txt
    except Exception as e:
        log("text error:", e)
        return ""


def click_exact(page, label):
    try:
        res = page.evaluate(
            "(lab) => {"
            "  const c=[...document.querySelectorAll("
            "'button,input[type=submit],input[type=button],a,[role=button]')];"
            "  const vis=e=>e.offsetParent!==null && !e.disabled;"
            "  const el=c.find(e=>((e.value||e.textContent||'').trim()===lab) && vis(e));"
            "  if(el){el.click(); return (el.id||el.tagName);}"
            "  return '';"
            "}", label)
        if res:
            log(f"Clicked '{label}' ({res}).")
            return True
        return False
    except Exception as e:
        log(f"click_exact('{label}') error:", e)
        return False


def click_weiter(page):
    if click_exact(page, "Weiter"):
        return True
    log("No exact 'Weiter' control found.")
    return False


def click_by_text(page, needle, timeout=8000):
    needle_l = needle.lower()
    for sel in ["button", "a", "[role=button]", "[role=option]", "label", "li"]:
        for el in page.query_selector_all(sel):
            try:
                t = (el.inner_text() or "").strip().lower()
            except Exception:
                continue
            if needle_l in t and el.is_visible():
                try:
                    el.scroll_into_view_if_needed(timeout=2000)
                    el.click(timeout=timeout)
                    return True
                except Exception:
                    try:
                        el.click(timeout=timeout, force=True)
                        return True
                    except Exception:
                        continue
    return False


def select_location(page):
    """On the Standortauswahl sub-step, select the (usually single) location."""
    try:
        radios = page.locator("input[type=radio]")
        if radios.count() > 0:
            try:
                radios.first.check(timeout=2000)
            except Exception:
                radios.first.click(timeout=2000)
            log("Selected location radio.")
            return True
    except Exception as e:
        log("location radio failed:", e)
    for txt in ["Außenstelle RWTH", "Templergraben", "Standort auswählen",
                "Auswählen", "auswählen", "Diesen Standort"]:
        if click_by_text(page, txt, timeout=1500):
            log(f"Selected location via '{txt}'.")
            return True
    return False


def expand_category(page, category):
    if not category:
        return
    try:
        loc = page.get_by_text(category, exact=False).first
        loc.scroll_into_view_if_needed(timeout=3000)
        loc.click(timeout=4000)
        log(f"Expanded category '{category}'.")
        page.wait_for_timeout(1500)
        dump_page(page, "step2b_category")
        return
    except Exception as e:
        log(f"get_by_text expand failed ({e}); trying JS fallback.")
    try:
        clicked = page.evaluate(
            """(cat) => {
                const els = [...document.querySelectorAll('button,a,li,div,span,h2,h3,[role=tab]')];
                const t = els.find(e => (e.textContent||'').trim().includes(cat));
                if (t) { t.click(); return true; }
                return false;
            }""", category)
        log(f"JS category expand clicked={clicked}")
        page.wait_for_timeout(1500)
        dump_page(page, "step2b_category")
    except Exception as e:
        log("JS expand failed:", e)


def select_concern(page, chosen):
    selected = False
    try:
        label = page.locator(f"label[aria-label={json.dumps(chosen)}]").first
        try:
            label.scroll_into_view_if_needed(timeout=4000)
        except Exception:
            pass

        input_id = label.get_attribute("for") or ""
        suffix = input_id.split("-")[-1] if input_id else ""
        log(f"Concern input id={input_id!r}, suffix={suffix!r}")

        plus_selectors = []
        if suffix:
            plus_selectors = [
                f"#button-plus-{suffix}",
                f"button[data-type='plus'][data-field='cnc-{suffix}']",
                f"#inputBox-{suffix} button[data-type='plus']",
            ]
        plus_selectors.append("button[data-type='plus']")
        for sel in plus_selectors:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0:
                    btn.click(timeout=4000)
                    selected = True
                    log(f"Clicked plus via '{sel}'.")
                    break
            except Exception as e:
                log(f"plus '{sel}' failed: {e}")

        if not selected and input_id:
            try:
                inp = page.locator(f"#{input_id}")
                inp.fill("1")
                inp.dispatch_event("input")
                inp.dispatch_event("change")
                selected = True
                log("Set quantity input to 1 (fallback).")
            except Exception as e:
                log("input fill failed:", e)
    except Exception as e:
        log("Concern selection error:", e)

    if not selected:
        click_by_text(page, chosen)

    page.wait_for_timeout(1000)
    try:
        ov = page.inner_text("body").lower()
        if f"0 anliegen {chosen.lower()} ausgewählt" in ov:
            log("WARNING: overview still shows 0 selected for this concern.")
        elif "anliegen" in ov and "ausgewählt" in ov:
            log("Overview shows a non-zero count — concern selected.")
    except Exception:
        pass


def list_clickables(page):
    out = []
    for sel, role in [("button", "button"), ("a", "link"),
                      ("[role=button]", "button"), ("[role=option]", "option"),
                      ("label", "label"), ("li", "li")]:
        try:
            for el in page.query_selector_all(sel):
                t = (el.inner_text() or "").strip()
                if t and len(t) < 160:
                    out.append((t, role))
        except Exception:
            pass
    seen, uniq = set(), []
    for t, r in out:
        if (t, r) not in seen:
            seen.add((t, r)); uniq.append((t, r))
    return uniq


def concern_matches(label, match_cfg):
    label_l = label.lower()
    for group in match_cfg.split(";"):
        words = [w.strip().lower() for w in group.split(",") if w.strip()]
        if words and all(w in label_l for w in words):
            return True
    return False


def text_has_negative(txt):
    t = txt.lower()
    return any(re.search(p, t) for p in NEGATIVE_PATTERNS)


TIME_RE = re.compile(r"\b\d{1,2}[:.]\d{2}\b")
DATE_RE = re.compile(r"\b\d{1,2}\.\d{1,2}\.(\d{2,4})?\b")
IGNORE_SLOT_TEXT = {"ok", "weiter", "abbrechen", "schliessen", "schließen", "x", "×"}


def find_available_dates(page):
    dates = []
    for sel in ["td.buchbar", "td.frei", ".buchbar", ".frei",
                "a.suggestion", ".suggestion", "button.timeslot",
                ".timeslot:not(.disabled)", "[class*='termin'] a",
                "a[href*='select']", "a[onclick*='termin']"]:
        try:
            for el in page.query_selector_all(sel):
                if not el.is_visible():
                    continue
                t = (el.inner_text() or el.get_attribute("aria-label") or "").strip()
                if t and t.lower() not in IGNORE_SLOT_TEXT:
                    dates.append(t)
        except Exception:
            pass
    try:
        for el in page.query_selector_all("a, button, td, li"):
            if not el.is_visible():
                continue
            t = (el.inner_text() or "").strip()
            if not t or t.lower() in IGNORE_SLOT_TEXT or len(t) > 40:
                continue
            if TIME_RE.search(t) or DATE_RE.search(t):
                dates.append(t)
    except Exception:
        pass
    seen, uniq = set(), []
    for d in dates:
        if d not in seen:
            seen.add(d); uniq.append(d)
    return uniq


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(
            locale="de-DE",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36"),
        )
        page = ctx.new_page()
        page.set_default_timeout(15000)

        log(f"Opening {BOOKING_URL}")
        page.goto(BOOKING_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        for c in ["Alle akzeptieren", "Akzeptieren", "Einverstanden",
                  "Nur notwendige", "Ablehnen", "Schließen", "OK"]:
            if click_by_text(page, c, timeout=2000):
                page.wait_for_timeout(800)
                break

        dump_page(page, "step1_authority")
        if DISCOVERY:
            log("=== STEP 1 clickables ===")
            for t, r in list_clickables(page):
                log(f"  [{r}] {t}")
        if click_by_text(page, AUTHORITY_MATCH):
            log(f"Clicked authority matching '{AUTHORITY_MATCH}'")
            page.wait_for_timeout(2000)
        else:
            log(f"Could not find authority '{AUTHORITY_MATCH}'.")

        click_weiter(page)
        page.wait_for_timeout(2000)

        dump_page(page, "step2_concern")
        clickables = list_clickables(page)
        if DISCOVERY or not CONCERN_MATCH:
            log("=== STEP 2 concern options ===")
            for t, r in clickables:
                log(f"  [{r}] {t}")
            if not CONCERN_MATCH:
                log("CONCERN_MATCH empty -> stopping after discovery.")
                browser.close()
                return "discovery"

        chosen = None
        for t, r in clickables:
            if concern_matches(t, CONCERN_MATCH):
                chosen = t
                break
        if not chosen:
            log(f"No concern matched '{CONCERN_MATCH}'.")
            dump_page(page, "step2_nomatch")
            browser.close()
            return "no-concern-match"

        expand_category(page, CONCERN_CATEGORY)
        log(f"Selecting concern: {chosen}")
        select_concern(page, chosen)
        page.wait_for_timeout(1000)

        click_weiter(page)
        page.wait_for_timeout(2000)

        for stp in range(8):
            body = page.inner_text("body")
            low = body.lower()
            m = re.search(r"schritt\s*(\d)\s*von\s*6", low)
            cur = int(m.group(1)) if m else 0

            if DISCOVERY:
                dump_page(page, f"step3_advance{stp}")
                log(f"[adv{stp}] current step={cur}")
                try:
                    for t, r in list_clickables(page)[:40]:
                        log(f"   [{r}] {t}")
                except Exception:
                    pass

            if text_has_negative(low):
                log("No-appointment message detected.")
                break
            if find_available_dates(page):
                log("Real slot candidate(s) detected.")
                break
            if cur >= 4:
                log(f"Reached step {cur} (personal data) — stopping.")
                break

            if click_exact(page, "OK"):
                log("Clicked OK (modal).")
                page.wait_for_timeout(1800)
                continue
            if "standortauswahl" in low:
                if select_location(page):
                    page.wait_for_timeout(1000)
                click_weiter(page)
                page.wait_for_timeout(2500)
                continue
            if not click_weiter(page):
                log("No further 'Weiter' — stopping advance.")
                break
            page.wait_for_timeout(2000)

        page.wait_for_timeout(1500)
        cal_text = dump_page(page, "step3_calendar")
        if DISCOVERY:
            log("=== FINAL page text (first 2500 chars) ===")
            log(cal_text[:2500])

        negative = text_has_negative(cal_text)
        dates = find_available_dates(page)
        available = (not negative) and len(dates) > 0

        log(f"Negative message present: {negative}")
        log(f"Selectable date/time cells: {len(dates)} -> {dates[:10]}")
        log(f"AVAILABLE = {available}")

        browser.close()
        return {"available": available, "dates": dates, "negative": negative}


def check_once(state):
    result = run()
    if isinstance(result, str):
        return False
    last_sig = state.get("sig")
    if result["available"]:
        sig = "AVAIL:" + "|".join(sorted(result["dates"])[:20])
        if sig != last_sig:
            dates_preview = ", ".join(result["dates"][:8]) or "see site"
            msg = ("🟢 Aachen Ausländerbehörde: appointment slot(s) AVAILABLE!\n"
                   f"Dates/times: {dates_preview}\n"
                   f"Book now: {BOOKING_URL}")
            notify(msg)
            state["sig"] = sig
        else:
            log("Still available, already notified — no repeat alert.")
    else:
        if last_sig and last_sig != "NONE":
            log("Slots gone — state reset.")
        state["sig"] = "NONE"
    state["last_check"] = int(time.time())
    save_state(state)
    return True


def main():
    checks   = int(os.environ.get("CHECKS_PER_RUN", "1"))
    interval = int(os.environ.get("CHECK_INTERVAL_SECONDS", "120"))
    state = load_state()
    for i in range(max(1, checks)):
        if i > 0:
            log(f"--- sleeping {interval}s before check {i + 1}/{checks} ---")
            time.sleep(interval)
        log(f"=== Check {i + 1}/{checks} ===")
        try:
            real = check_once(state)
        except PWTimeout as e:
            log("Timeout this check:", e)
            continue
        except Exception as e:
            log("Error this check:", e)
            continue
        if not real:
            break


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("Fatal error:", e)
        sys.exit(0)
