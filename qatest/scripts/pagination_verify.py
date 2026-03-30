import os
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


DETECT_JS = r"""
() => {
  const isVisible = (el) => {
    try {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      if (!st || st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity || 1) === 0) return false;
      const r = el.getBoundingClientRect();
      if (!r || r.width < 6 || r.height < 6) return false;
      if (r.bottom < 0 || r.right < 0 || r.top > window.innerHeight || r.left > window.innerWidth) return false;
      return true;
    } catch (e) { return false; }
  };
  const pickText = (el) => {
    try { return String((el.innerText || el.textContent || '')).trim(); } catch (e) { return ''; }
  };
  const toInt = (s) => {
    const m = String(s || '').match(/\d+/);
    return m ? Number(m[0]) : null;
  };
  const findActivePageNum = () => {
    const el = document.querySelector('[aria-current="page"]');
    if (el && isVisible(el)) return toInt(pickText(el));
    return null;
  };
  const findTotalByMaxPageItem = () => {
    const nodes = Array.from(document.querySelectorAll('a,button,li,span')).filter(isVisible);
    let max = null;
    for (const n of nodes) {
      const t = pickText(n);
      if (!t) continue;
      if (t.length > 6) continue;
      const v = toInt(t);
      if (!v || v < 1 || v > 500) continue;
      if (max === null || v > max) max = v;
    }
    return max;
  };
  const findNextButton = () => {
    const cands = Array.from(document.querySelectorAll('button,a,[role=button]')).filter(isVisible);
    const score = (el) => {
      const t = pickText(el).toLowerCase();
      const al = String(el.getAttribute('aria-label') || '').toLowerCase();
      const ti = String(el.getAttribute('title') || '').toLowerCase();
      const s = [t, al, ti].join(' ');
      if (!s) return 0;
      if (s.includes('下一页') || s.includes('next') || s.includes('›') || s.includes('»') || s.includes('下页')) return 10;
      return 0;
    };
    let best = null;
    let bestS = 0;
    for (const el of cands) {
      const sc = score(el);
      if (sc > bestS) { best = el; bestS = sc; }
    }
    return best;
  };
  const isDisabled = (el) => {
    try {
      if (!el) return false;
      const aria = String(el.getAttribute('aria-disabled') || '');
      if (aria === 'true') return true;
      if (el.hasAttribute('disabled')) return true;
      const cls = String(el.className || '').toLowerCase();
      if (cls.includes('disabled')) return true;
      return false;
    } catch (e) { return false; }
  };
  const current = findActivePageNum();
  const total = findTotalByMaxPageItem();
  const nextBtn = findNextButton();
  const nextDisabled = nextBtn ? isDisabled(nextBtn) : null;
  return { current, total, nextDisabled };
}
"""

CLICK_NEXT_JS = r"""
() => {
  const isVisible = (el) => {
    try {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      if (!st || st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity || 1) === 0) return false;
      const r = el.getBoundingClientRect();
      if (!r || r.width < 6 || r.height < 6) return false;
      if (r.bottom < 0 || r.right < 0 || r.top > window.innerHeight || r.left > window.innerWidth) return false;
      return true;
    } catch (e) { return false; }
  };
  const pickText = (el) => {
    try { return String((el.innerText || el.textContent || '')).trim(); } catch (e) { return ''; }
  };
  const isDisabled = (el) => {
    try {
      if (!el) return false;
      const aria = String(el.getAttribute('aria-disabled') || '');
      if (aria === 'true') return true;
      if (el.hasAttribute('disabled')) return true;
      const cls = String(el.className || '').toLowerCase();
      if (cls.includes('disabled')) return true;
      return false;
    } catch (e) { return false; }
  };
  const cands = Array.from(document.querySelectorAll('button,a,[role=button]')).filter(isVisible);
  const score = (el) => {
    const t = pickText(el).toLowerCase();
    const al = String(el.getAttribute('aria-label') || '').toLowerCase();
    const ti = String(el.getAttribute('title') || '').toLowerCase();
    const s = [t, al, ti].join(' ');
    if (!s) return 0;
    if (s.includes('下一页') || s.includes('next') || s.includes('›') || s.includes('»') || s.includes('下页')) return 10;
    return 0;
  };
  let best = null;
  let bestS = 0;
  for (const el of cands) {
    const sc = score(el);
    if (sc > bestS) { best = el; bestS = sc; }
  }
  if (!best) return { clicked: false, reason: 'not_found' };
  if (isDisabled(best)) return { clicked: false, reason: 'disabled' };
  best.click();
  return { clicked: true };
}
"""


async def main():
    html_path = Path(__file__).with_name("pagination_test_page.html").resolve()
    url = html_path.as_uri()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await ctx.new_page()
        await page.goto(url)
        s1 = await page.evaluate(DETECT_JS)
        print("before", s1)
        assert int(s1["current"]) == 1
        c = await page.evaluate(CLICK_NEXT_JS)
        print("click", c)
        for _ in range(10):
            await page.wait_for_timeout(100)
            s2 = await page.evaluate(DETECT_JS)
            if int(s2["current"]) == 2:
                break
        s2 = await page.evaluate(DETECT_JS)
        print("after", s2)
        assert int(s2["current"]) == 2
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

