"""Real-browser page reader (optional; needs `pip install playwright`).

Opens pages in an actual Chrome/Edge/Chromium on this computer, the way you would:
* JavaScript runs, so pages that build their text in the browser (Linktree, profile bios, modern blogs) are read.
* A persistent profile (.browser_profile/) keeps cookies between runs, like your own browser.
* Visible mode shows the window. If a site asks "verify you are human" or shows a CAPTCHA, the app waits for YOU
  to complete it, then carries on.

Deliberately NOT included: CAPTCHA-solving services, hiding that the browser is automated, fake fingerprints or
rotating proxies. When a site wants a person, the person running the tool does it.
"""

from __future__ import annotations

import queue
import re
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path

from .util import log

CHALLENGE_RE = re.compile(
    r"(just a moment|checking (if the site connection is secure|your browser)|verify (that )?you are (a )?human|"
    r"are you a robot|i'?m not a robot|captcha|attention required|press (and|&) hold|unusual traffic|"
    r"security check to access|enable javascript and cookies to continue|ddos protection by)",
    re.I,
)


@dataclass
class BrowserPage:
    """Shaped like a requests.Response so the reader can treat both the same way."""
    url: str
    text: str
    status_code: int = 200
    age_gate: str = ""                     # "", "click", "off", "strict", "challenge"
    headers: dict = field(default_factory=lambda: {"content-type": "text/html; charset=utf-8"})


class BrowserUnavailable(RuntimeError):
    pass


class BrowserFetcher:
    """One browser, driven from one worker thread (Playwright's sync API is single-threaded). Other threads
    queue page loads and wait for the result."""

    def __init__(self, profile_dir: Path, visible: bool = False, confirm_adult: bool = False,
                 human_wait: int = 180):
        self.profile_dir = profile_dir
        self.visible = visible
        self.confirm_adult = confirm_adult
        self.human_wait = human_wait
        self._q: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self.error: str = ""
        self.name = ""
        self._thread = threading.Thread(target=self._run, daemon=True, name="browser")
        self._thread.start()
        self._ready.wait(90)
        if self.error:
            raise BrowserUnavailable(self.error)

    # ── worker thread
    def _run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.error = ("the real-browser reader needs Playwright: pip install playwright "
                          "(and, if you don't have Chrome or Edge: python -m playwright install chromium)")
            self._ready.set()
            return
        pw = ctx = None
        try:
            pw = sync_playwright().start()
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            opts = dict(user_data_dir=str(self.profile_dir), headless=not self.visible, locale="en-US",
                        viewport={"width": 1280, "height": 900})
            last = None
            for channel in ("chrome", "msedge", None):  # your installed Chrome/Edge first, then Playwright's Chromium
                try:
                    ctx = pw.chromium.launch_persistent_context(channel=channel, **opts) if channel else \
                        pw.chromium.launch_persistent_context(**opts)
                    self.name = channel or "chromium"
                    break
                except Exception as e:  # noqa: BLE001
                    last = e
            if ctx is None:
                raise BrowserUnavailable(
                    f"no browser could be started ({str(last).splitlines()[0] if last else 'unknown error'}). "
                    "Install Chrome, or run: python -m playwright install chromium")
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            self._ready.set()
            if pw:
                pw.stop()
            return
        self._ready.set()
        try:
            while True:
                item = self._q.get()
                if item is None:
                    break
                url, fut = item
                try:
                    fut.set_result(self._load(ctx, url))
                except Exception as e:  # noqa: BLE001
                    fut.set_exception(e)
        finally:
            try:
                ctx.close()
                pw.stop()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _settle(page, ms: int = 6000) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=ms)
        except Exception:  # noqa: BLE001 – busy pages never go idle; what has loaded is enough
            pass

    @staticmethod
    def _visible_text(page) -> str:
        try:
            return page.inner_text("body", timeout=5000)[:4000]
        except Exception:  # noqa: BLE001
            return ""

    def _is_challenge(self, page) -> bool:
        try:
            title = page.title()
        except Exception:  # noqa: BLE001
            title = ""
        text = self._visible_text(page)
        return bool(CHALLENGE_RE.search(title)) or (len(text) < 1500 and bool(CHALLENGE_RE.search(text)))

    def _wait_for_person(self, page, why: str, done) -> bool:
        """Visible mode: let the user finish a check in the window; returns True once `done()` is true."""
        log(f"  ⏸ {page.url}: {why} – please complete it in the browser window "
            f"(waiting up to {self.human_wait // 60} min)…")
        try:
            page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        end = time.time() + self.human_wait
        while time.time() < end:
            page.wait_for_timeout(2000)
            if done():
                log("  ▶ thanks – continuing")
                self._settle(page)
                return True
        return False

    def _click_confirm(self, page) -> bool:
        from .research import CONFIRM_WORDS, DENY_WORDS

        try:
            for box in page.locator("input[type=checkbox]:visible").all()[:5]:
                box.check(timeout=2000)
        except Exception:  # noqa: BLE001
            pass
        cands = page.locator("button:visible, a:visible, input[type=submit]:visible, input[type=button]:visible, "
                             "[role=button]:visible")
        for i in range(min(cands.count(), 80)):
            el = cands.nth(i)
            try:
                label = " ".join(filter(None, [el.inner_text(timeout=1000), el.get_attribute("value"),
                                               el.get_attribute("aria-label"), el.get_attribute("title")]))
            except Exception:  # noqa: BLE001
                continue
            if label and len(label) < 80 and CONFIRM_WORDS.search(label) and not DENY_WORDS.search(label):
                try:
                    el.click(timeout=4000)
                    self._settle(page)
                    return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    def _load(self, ctx, url: str) -> BrowserPage:
        from .research import age_gate_kind

        page = ctx.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            status = resp.status if resp else 200
            self._settle(page)
            if self._is_challenge(page):
                if not (self.visible and self._wait_for_person(page, "the site asks to verify you're human",
                                                               lambda: not self._is_challenge(page))):
                    return BrowserPage(page.url, page.content(), status, "challenge")
                status = 200
            gate = age_gate_kind(page.content())
            if gate == "strict":
                return BrowserPage(page.url, page.content(), status, "strict")
            if gate == "click":
                if not self.confirm_adult:
                    return BrowserPage(page.url, page.content(), status, "off")
                if not (self._click_confirm(page) and not age_gate_kind(page.content())):
                    if not (self.visible and self._wait_for_person(page, "the site asks you to confirm you're 18+",
                                                                   lambda: not age_gate_kind(page.content()))):
                        return BrowserPage(page.url, page.content(), status, "click")
                if page.url.rstrip("/") != url.rstrip("/") and age_gate_kind(page.content()) == "":
                    try:  # some gates land on the home page afterwards – go back to the page that was asked for
                        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                        self._settle(page)
                    except Exception:  # noqa: BLE001
                        pass
                status = 200
            for _ in range(3):  # lazy-loaded text appears as a reader scrolls
                try:
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(400)
                except Exception:  # noqa: BLE001
                    break
            return BrowserPage(page.url, page.content(), status if status else 200, "")
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

    # ── called from any thread
    def load(self, url: str, timeout: int = 300) -> BrowserPage:
        fut: Future = Future()
        self._q.put((url, fut))
        return fut.result(timeout=timeout + (self.human_wait if self.visible else 0))

    def close(self) -> None:
        self._q.put(None)
        self._thread.join(timeout=15)
