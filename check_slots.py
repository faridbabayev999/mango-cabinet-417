#!/usr/bin/env python3
"""
Aachen Ausländerbehörde appointment-slot checker.

Walks the VOIS|TEVIS booking wizard at the StädteRegion Aachen Ausländeramt,
detects whether any appointment slots are currently bookable for the chosen
"Anliegen" (concern), and sends a Telegram + WhatsApp (CallMeBot) alert ONLY
when slots are available — and only once per change (no spam).

Designed to run on GitHub Actions. To get a finer cadence than GitHub's 5-min
minimum cron, the run does several checks internally (CHECKS_PER_RUN), sleeping
CHECK_INTERVAL_SECONDS between them (e.g. 2 checks * 120s ≈ one check / 2 min).

First-run calibration: set DISCOVERY=1 to make the bot print every option it
sees at each step (and save screenshots) instead of trying to detect slots.
Use that output to pick the exact CONCERN_MATCH text for RWTH students.
"""

import os
import re
import sys
import json
import time
import pathlib
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ----------------------------------------------------------------------------
# Configuration (all via environment variables / GitHub Secrets & Variables)
# ----------------------------------------------------------------------------
BOOKING_URL     = os.environ.get("BOOKING_URL", "https://termine.staedteregion-aachen.de/auslaenderamt/")
AUTHORITY_MATCH = os.environ.get("AUTHORITY_MATCH", "Ausländer")  # step 1 button text
# Groups separated by ';'; words in a group separated by ','. A concern label
# matches if ALL words of ANY group are substrings of it.
# Example: "Aufenthalt,Studierende" or "RWTH" or "Verlängerung,Studierende;Aufenthaltstitel,RWTH"
CONCERN_MATCH   = os.environ.get("CONCERN_MATCH", "")

DISCOVERY = os.environ.get("DISCOVERY", "") not in ("", "0", "false", "False")
HEADLESS  = os.environ.get("HEADLESS", "1") not in ("0", "false", "False")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
ART_DIR    = pathlib.Path(os.environ.get("ARTIFACT_DIR", "artifacts"))
ART_DIR.mkdir(parents=True, exist_ok=True)

# Notification secrets
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
CALLMEBOT_PHONE    = os.environ.get("CALLMEBOT_PHONE", "")   # e.g. +491234567890
CALLMEBOT_APIKEY   = os.environ.get("CALLMEBOT_APIKEY", "")

# German phrases that mean "no appointments available"
NEGATIVE_PATTERNS = [
    r"kein[e]?\s+frei[en]*\s+termin",
    r"keine\s+termine",
    r"derzeit\s+keine",
    r"aktuell\s+keine",
    r"leider\s+sind\s+(aktuell|derzeit|momentan)?\s*keine",
    r"no\s+appointments?\s+available",
    r"keine\s+freien\s+zeiten",
]


# ----------------------------------------------------------------------------
# Notifications
# ----------------------------------------------------------------------------
def log(*a):
    print(*a, flush=True)


def notify_telegram(text):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
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
            "phone": CALLMEBOT_PHONE,
            "text": text,
            "apikey": CALLMEBOT_APIKEY,
        })
        url = f"https://api.callmebot.com/whatsapp.php?{params}"
        urllib.request.urlopen(url, timeout=20)
        log("WhatsApp alert sent.")
    except Exception as e:
        log("WhatsApp error:", e)


def notify(text):
    log("NOTIFY:", text)
    notify_telegram(text)
    notify_whatsapp(text)


# ----------------------------------------------------------------------------
# State (dedupe so we only alert on change)
# ----------------------------------------------------------------------------
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


# ----------------------------------------------------------------------------
# Wizard helpers
# ----------------------------------------------------------------------------
def dump_page(page, tag):
    """Save screenshot + text for calibration / debugging."""
    try:
        page.screenshot(path=str(ART_DIR / f"{tag}.png"), full_page=True)
    except Exception as e:
        log("screenshot error:", e)
    try:
        txt = page.inner_text("body")
        (ART_DIR / f"{tag}.txt").write_text(txt, encoding="utf-8")
        return txt
    except Exception as e:
        log("text error:", e)
        return ""


def list_clickables(page):
    """Return [(text, role)] of buttons/links/options on the page."""
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
        k = (t, r)
        if k not in seen:
            seen.add(k)
            uniq.append((t, r))
    return uniq


def click_by_text(page, needle, timeout=8000):
    """Click the first visible element containing `needle` (case-insensitive)."""
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


def find_available_dates(page):
    """Heuristic: collect text of enabled/selectable day or time cells."""
    dates = []
    candidate_selectors = [
        "td.buchbar", "td.frei", ".available", ".bookable",
        "button[aria-disabled=false]", "a.suggestion", ".suggestion",
        "td[role=gridcell]:not([aria-disabled=true])",
        "button.timeslot", ".timeslot:not(.disabled)",
    ]
    for sel in candidate_selectors:
        try:
            for el in page.query_selector_all(sel):
                if not el.is_visible():
                    continue
                t = (el.inner_text() or el.get_attribute("aria-label") or "").strip()
                if t:
                    dates.append(t)
        except Exception:
            pass
    seen, uniq = set(), []
    for d in dates:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


# ----------------------------------------------------------------------------
# One pass through the wizard
# ----------------------------------------------------------------------------
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

        # Dismiss cookie banner if present
        for c in ["Alle akzeptieren", "Akzeptieren", "Einverstanden",
                  "Nur notwendige", "Ablehnen", "Schließen", "OK"]:
            if click_by_text(page, c, timeout=2000):
                page.wait_for_timeout(800)
                break

        # ---- Step 1: choose authority -------------------------------------
        dump_page(page, "step1_authority")
        if DISCOVERY:
            log("=== STEP 1 clickables ===")
            for t, r in list_clickables(page):
                log(f"  [{r}] {t}")
        if click_by_text(page, AUTHORITY_MATCH):
            log(f"Clicked authority matching '{AUTHORITY_MATCH}'")
            page.wait_for_timeout(2000)
        else:
            log(f"Could not find authority '{AUTHORITY_MATCH}'. (May be single-authority.)")

        click_by_text(page, "Weiter", timeout=2500)
        page.wait_for_timeout(2000)

        # ---- Step 2: choose concern ("Anliegen") --------------------------
        dump_page(page, "step2_concern")
        clickables = list_clickables(page)
        if DISCOVERY or not CONCERN_MATCH:
            log("=== STEP 2 concern options (pick one for CONCERN_MATCH) ===")
            for t, r in clickables:
                log(f"  [{r}] {t}")
            if not CONCERN_MATCH:
                log("\nCONCERN_MATCH is empty -> stopping after discovery. "
                    "Set CONCERN_MATCH and re-run.")
                browser.close()
                return "discovery"

        chosen = None
        for t, r in clickables:
            if concern_matches(t, CONCERN_MATCH):
                chosen = t
                break
        if not chosen:
            log(f"No concern matched '{CONCERN_MATCH}'. Options were:")
            for t, r in clickables:
                log("   -", t)
            dump_page(page, "step2_nomatch")
            browser.close()
            return "no-concern-match"

        log(f"Selecting concern: {chosen}")
        # TEVIS often needs a quantity '+' next to the concern, then 'Weiter'.
        if not click_by_text(page, "+", timeout=2000):
            click_by_text(page, chosen)
        page.wait_for_timeout(1200)
        click_by_text(page, "Weiter", timeout=4000)
        page.wait_for_timeout(2500)

        # ---- Step 3: calendar / suggestions -------------------------------
        page.wait_for_timeout(1500)
        cal_text = dump_page(page, "step3_calendar")
        if DISCOVERY:
            log("=== STEP 3 page text (first 1500 chars) ===")
            log(cal_text[:1500])

        negative = text_has_negative(cal_text)
        dates = find_available_dates(page)
        # Conservative: only "available" when there are selectable cells AND no
        # explicit "no appointments" message. Avoids false alarms.
        available = (not negative) and len(dates) > 0

        log(f"Negative message present: {negative}")
        log(f"Selectable date/time cells found: {len(dates)} -> {dates[:10]}")
        log(f"AVAILABLE = {available}")

        browser.close()
        return {"available": available, "dates": dates, "negative": negative}


# ----------------------------------------------------------------------------
# Check + notify (with dedupe), and the multi-check loop
# ----------------------------------------------------------------------------
def check_once(state):
    """Run one check and send alerts based on change vs. stored state.
    Returns True if this was a real check (not discovery/no-match)."""
    result = run()
    if isinstance(result, str):  # discovery / no-match
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
            log("Still available, already notified for this set — no repeat alert.")
    else:
        if last_sig and last_sig != "NONE":
            log("Slots gone — state reset.")
        state["sig"] = "NONE"

    state["last_check"] = int(time.time())
    save_state(state)
    return True


def main():
    # Finer cadence than GitHub's 5-min cron: several checks per run, sleeping
    # CHECK_INTERVAL_SECONDS between them (2 checks * 120s ≈ one check / 2 min).
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
            log("Timeout this check (site slow/layout changed):", e)
            continue
        except Exception as e:
            log("Error this check:", e)
            continue
        if not real:
            break  # discovery mode or no concern match — no point looping


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("Fatal error:", e)
        sys.exit(0)  # never fail the Action on a transient error
