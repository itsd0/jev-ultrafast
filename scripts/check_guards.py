"""Local-browser freshness/execution regressions. No model calls or external websites."""

import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from jev_ultrafast.browser import Browser, StalePage, combo_options_expression

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

HTML = """<!doctype html><title>Guard checks</title>
<style>body{margin:30px}button{width:180px;height:50px}#outside{position:absolute;top:3000px}</style>
<p id="context">Cart total: $10</p>
<button id="target" onclick="window.clicks=(window.clicks||0)+1">Continue</button>
<label>City<input id="field" value="Zurich"></label>
<label><input id="toggle" type="checkbox">Refundable</label>
<select aria-label="Category"><option>All</option><option>Design</option></select>
<p id="outside">Unrelated offscreen text</p>"""


def main():
    browser = Browser("data:text/html," + quote(HTML))
    passed = []
    try:
        page = browser.observe(screenshot=False)
        action = next(a for a in page["actions"] if a["label"] == "Continue")
        browser.evaluate("document.querySelector('#target').style.transform='translateX(200px)'")
        assert browser.fresh(page), "Movement should use fresh geometry, not another model call"
        browser.act(action, page)
        assert browser.evaluate("window.clicks") == 1
        passed.append("moving target clicked at its current location")

        browser.evaluate("document.querySelector('#outside').textContent='Updated outside the viewport'")
        assert browser.fresh(page)
        passed.append("unrelated offscreen text does not invalidate")

        stays_fresh = {
            "visible context": "document.querySelector('#context').textContent='Cart total: $100'",
            "field property": "document.querySelector('#field').value='London'",
            "checkbox property": "document.querySelector('#toggle').checked=true",
            "read-only field": "document.querySelector('#field').readOnly=true",
            "dropdown option": "document.querySelector('select').options[1].text='Coastal'",
        }
        invalidates = {
            "accessible label": "document.querySelector('#target').setAttribute('aria-label','Delete account')",
            "replaced node": "document.querySelector('#target').outerHTML=document.querySelector('#target').outerHTML",
        }
        for label in stays_fresh:
            browser.evaluate("document.querySelector('#target').style.display='block'; "
                             "document.querySelector('#target').disabled=false")
            page = browser.observe(screenshot=False)
            target = next(a for a in page["actions"] if a["label"] == "Continue")
            browser.evaluate(stays_fresh[label])
            assert browser.fresh(page, target), label
            assert browser.fresh(page), label
            passed.append(label + " keeps the target fresh")
        for label in invalidates:
            browser.evaluate("document.querySelector('#target').style.display='block'; "
                             "document.querySelector('#target').disabled=false")
            page = browser.observe(screenshot=False)
            target = next(a for a in page["actions"] if a["label"] in {"Continue", "Delete account"})
            browser.evaluate(invalidates[label])
            assert not browser.fresh(page, target), label
            passed.append(label + " invalidates the target")
        for label, expression in {
            "disabled target": "document.querySelector('#target').disabled=true",
            "hidden target": "document.querySelector('#target').style.display='none'",
        }.items():
            page = browser.observe(screenshot=False)
            target = next(a for a in page["actions"] if a["label"] in {"Continue", "Delete account"})
            browser.evaluate(expression)
            assert browser.fresh(page, target), label
            try:
                browser.act(target, page)
                raise AssertionError(label + " was not refused")
            except StalePage:
                pass
            passed.append(label + " stays fresh but execution refuses it")
            browser.evaluate("document.querySelector('#target').disabled=false; "
                             "document.querySelector('#target').style.display='block'")

        browser.evaluate("document.querySelector('#target').disabled=false; "
                         "document.querySelector('#target').style.display='block'")
        page = browser.observe(screenshot=False)
        action = next(a for a in page["actions"] if a["label"] == "Delete account")
        # A textless overlay does not alter the model's semantic state, but must block a click.
        browser.evaluate("const cover=document.createElement('div'); "
                         "cover.style.cssText='position:fixed;inset:0;z-index:9999;background:white'; "
                         "document.body.append(cover)")
        assert browser.fresh(page)
        try:
            browser.act(action, page)
        except (RuntimeError, StalePage):
            pass
        else:
            raise AssertionError("Covered target was clicked")
        assert browser.evaluate("window.clicks") == 1
        passed.append("overlay blocked before input")

        browser.evaluate("document.body.innerHTML=" + repr("""
          <form><p id="price">Total $10</p>
          <button type="button" id="buy">Buy</button>
          <label>Search <input id="query" role="combobox" aria-controls="suggestions"></label>
          <div role="listbox" id="suggestions"></div>
          <label><input id="check" type="checkbox">Enabled</label>
          <label><input id="radio" type="radio">Choice</label>
          <input id="readonly" aria-label="Read only" readonly>
          <input id="secret" type="password" value="never expose this">
          <button id="off" disabled>Disabled</button>
          <select id="category" aria-label="Category">
            <option>All</option><option>Design</option><option disabled>Unavailable</option>
          </select></form><aside id="unrelated">News</aside>
        """))
        page = browser.observe(screenshot=False)
        buy = next(a for a in page["actions"] if a["label"] == "Buy")
        browser.evaluate("document.querySelector('#unrelated').textContent='New unrelated news'")
        assert browser.fresh(page, buy)
        assert browser.fresh(page)
        passed.append("unrelated updates keep the target and document identity fresh")
        for label, expression in {
            "nearby price": "document.querySelector('#price').textContent='Total $100'",
            "form value": "document.querySelector('#query').value='changed'",
            "form toggle": "document.querySelector('#check').checked=true",
        }.items():
            page = browser.observe(screenshot=False)
            buy = next(a for a in page["actions"] if a["label"] == "Buy")
            browser.evaluate(expression)
            assert browser.fresh(page, buy), label
            passed.append(label + " keeps the target fresh")
        page = browser.observe(screenshot=False)
        buy = next(a for a in page["actions"] if a["label"] == "Buy")
        browser.evaluate("document.querySelector('#buy').outerHTML=document.querySelector('#buy').outerHTML")
        assert not browser.fresh(page, buy)
        passed.append("target replacement invalidates the target")
        page = browser.observe(screenshot=False)
        assert browser.fresh(page, {"kind": "scroll", "label": "Scroll down"})
        passed.append("scroll is gated on document identity, not page content")
        width = browser.evaluate("window.innerWidth")
        browser.call(
            "Emulation.setDeviceMetricsOverride", width=width + 8, height=800,
            deviceScaleFactor=1, mobile=False,
        )
        try:
            buy = next(a for a in page["actions"] if a["label"] == "Buy")
            assert browser.fresh(page, buy) and browser.fresh(page)
        finally:
            browser.call("Emulation.clearDeviceMetricsOverride")
        passed.append("8px viewport jitter keeps the page fresh")

        page = browser.observe(screenshot=False)
        actions = page["actions"]
        for role in ("checkbox", "radio"):
            assert {a["kind"] for a in actions if a.get("role") == role} == {"check"}
        assert {a["kind"] for a in actions if a["label"] == "Read only"} == {"click"}
        assert not any(a["label"] == "Disabled" or a.get("value") == "never expose this" for a in actions)
        assert [a["value"] for a in actions if a["kind"] == "select"] == ["Design"]
        passed.append("native controls expose only supported operations and safe values")

        select = next(a for a in actions if a["kind"] == "select")
        browser.act(select, page)
        assert browser.evaluate("document.querySelector('#category').value") == "Design"
        passed.append("native dropdown selects an observed option")

        browser.evaluate("document.querySelector('#query').addEventListener('input',()=>setTimeout(()=>{"
                         "document.querySelector('#suggestions').innerHTML='<div role=option>Generated</div>'"
                         "},60))")
        page = browser.observe(screenshot=False)
        field = next(a for a in page["actions"] if a["kind"] == "fill")
        browser.act(field, page, text="Generated")
        page = browser.observe(screenshot=False)
        value = browser.evaluate("document.querySelector('#query').value")
        assert value == "Generated", repr(value)
        assert any(a.get("role") == "option" for a in page["actions"])
        passed.append("real text input waits for asynchronous combobox suggestions")

        browser.evaluate("document.body.innerHTML=" + repr('<label>Code <input id="twin"></label>'))
        page = browser.observe(screenshot=False)
        field = next(a for a in page["actions"] if a["kind"] == "fill")
        browser.evaluate(
            "document.querySelector('#twin').addEventListener('focus',()=>{"
            "const old=document.querySelector('#twin');const next=document.createElement('input');"
            "next.id='twin';old.replaceWith(next);next.focus();})"
        )
        result = browser.act(field, page, text="K7")
        assert result.get("text_verified") is True, result
        assert browser.evaluate("document.querySelector('#twin').value") == "K7"
        assert browser.evaluate("document.querySelectorAll('input').length") == 1
        passed.append("replaced field verifies through the focused twin")
        browser.call("Page.navigate", url="about:blank")
        assert not browser.fresh(page, field)
        passed.append("navigation invalidates the old document")

        browser.call("Page.navigate", url="data:text/html," + quote((FIXTURES / "shadow.html").read_text()))
        browser.wait_until_ready()
        page = browser.observe(screenshot=False)
        labels = [a["label"] for a in page["actions"]]
        assert "Inside shadow" in labels, labels
        assert "Hidden control" not in labels, labels
        assert any(a.get("possible_closed_shadow") and a["label"] == "Closed host" for a in page["actions"])
        shadow = next(a for a in page["actions"] if a["label"] == "Inside shadow")
        browser.act(shadow, page)
        assert browser.evaluate("window.shadowClicks") == 1
        passed.append("open shadow button is hit-tested and clicked")

        browser.call("Page.navigate", url="data:text/html," + quote((FIXTURES / "iframe.html").read_text()))
        browser.wait_until_ready()
        page = browser.observe(screenshot=False)
        inner = next(a for a in page["actions"] if a["label"] == "Inside frame")
        assert inner.get("frame") not in {None, "f0"}
        assert str(inner["id"]).startswith(str(inner["frame"]) + ":")
        browser.evaluate(
            "document.querySelector('#child').contentDocument.querySelector('button').onclick="
            "()=>{window.frameClicks=(window.frameClicks||0)+1}"
        )
        page = browser.observe(screenshot=False)
        inner = next(a for a in page["actions"] if a["label"] == "Inside frame")
        browser.act(inner, page)
        assert browser.evaluate("window.frameClicks") == 1
        passed.append("same-origin iframe control keeps a frame id and receives the click")

        upload_path = Path("/tmp/jev-ultrafast-upload.txt")
        upload_path.write_text("resume")
        browser.config["files"] = {"resume": str(upload_path)}
        browser.call("Page.navigate", url="data:text/html," + quote((FIXTURES / "upload.html").read_text()))
        browser.wait_until_ready()
        page = browser.observe(screenshot=False)
        encoded = json.dumps(page["actions"])
        assert "resume" in encoded and str(upload_path) not in encoded
        assert not any(a.get("value") == "never expose this" for a in page["actions"])
        upload = next(a for a in page["actions"] if a.get("upload_key") == "resume")
        browser.act(upload, page)
        assert browser.evaluate("document.querySelector('#resume').files.length") == 1
        passed.append("file input accepts only the allowlisted key")

        browser.call("Page.navigate", url="data:text/html," + quote((FIXTURES / "popup.html").read_text()))
        browser.wait_until_ready()
        page = browser.observe(screenshot=False)
        opener = next(a for a in page["actions"] if a["label"] == "Open tab")
        browser.act(opener, page)
        opened = browser.collect_new_tabs()
        if opened:
            tab = browser.activate(opened[-1])
            assert tab["target"] not in opened
            assert opened[-1].startswith("t")
            passed.append("popup tab is tracked with a code-owned id")
        else:
            passed.append("popup was blocked by the browser; opener button still clicked")

        browser.call("Page.navigate", url="data:text/html," + quote((FIXTURES / "scroll.html").read_text()))
        browser.wait_until_ready()
        page = browser.observe(screenshot=False)
        nested = [
            a for a in page["actions"]
            if a.get("kind") == "scroll" and a.get("node") and "Tall" in a.get("label", "")
        ]
        assert nested, [a["label"] for a in page["actions"] if a.get("kind") == "scroll"]
        down = next(a for a in nested if a["delta"] > 0)
        before = browser.evaluate("document.querySelector('#box').scrollTop")
        browser.act(down, page)
        after = browser.evaluate("document.querySelector('#box').scrollTop")
        assert after > before, (before, after)
        passed.append("nested container scrolls from its own center")

        browser.call("Page.navigate", url="data:text/html," + quote((FIXTURES / "keyboard.html").read_text()))
        browser.wait_until_ready()
        page = browser.observe(screenshot=False)
        assert any(a.get("key") == "Enter" for a in page["actions"])
        field = next(a for a in page["actions"] if a.get("kind") == "fill" and a.get("label") == "Code")
        browser.act(field, page, text="ab")
        assert browser.evaluate("document.querySelector('#typed').value") == "ab"
        passed.append("keydown-only field receives a verified retype")

        browser.call("Page.navigate", url="data:text/html," + quote((FIXTURES / "select.html").read_text()))
        browser.wait_until_ready()
        page = browser.observe(screenshot=False)
        large = [a for a in page["actions"] if a.get("kind") == "select_option"]
        assert len(large) == 1, [a.get("kind") for a in page["actions"]]
        assert large[0].get("option_count", 0) > 12
        assert "Option 150" not in json.dumps(large)
        chosen = {**large[0], "kind": "select", "value": "42"}
        browser.act(chosen, page)
        assert browser.evaluate("document.querySelector('#countries').value") == "42"
        passed.append("long select is one target and selects an observed value")

        browser.call("Page.navigate", url="data:text/html," + quote((FIXTURES / "widgets.html").read_text()))
        browser.wait_until_ready()
        page = browser.observe(screenshot=False)
        painted = next(a for a in page["actions"] if a.get("label") == "Painted popup")
        browser.act(painted, page)
        assert browser.evaluate("window.paintedClicks") == 1
        passed.append("aria-hidden control that paints on top is indexed and clicked")
        nav_link = next(a for a in page["actions"] if a.get("label") == "Menu link")
        assert nav_link.get("nav") is True
        passed.append("nav-region link is flagged")

        opener = next(a for a in page["actions"] if a["label"] == "Open dialog")
        browser.act(opener, page)
        page = browser.observe(screenshot=False)
        labels = [a["label"] for a in page["actions"]]
        assert "Background" not in labels and "Painted popup" not in labels, labels
        assert "Close" in labels, labels
        passed.append("open modal scopes choices to its own controls")
        nonstop = next(a for a in page["actions"] if a["label"] == "Set Nonstop only (Stops)")
        assert nonstop["kind"] == "check" and nonstop.get("checked") == "false"
        business = next(a for a in page["actions"] if a["label"] == "Set Business (Class)")
        assert business.get("checked") == "false"
        clear_any = next(a for a in page["actions"] if a["label"] == "Clear Any stops (Stops)")
        assert clear_any.get("desired") is False
        passed.append("transparent radios index with names, groups, states, and Clear")
        day = next(a for a in page["actions"] if a["label"] == "13")
        assert day.get("in_grid") is True
        passed.append("calendar day indexes with its grid flag")

        before = browser.evaluate("document.querySelectorAll('input[name=stops]:checked').length")
        browser.act(nonstop, page)
        after = browser.evaluate("document.querySelector(\"#r2\").checked")
        assert (before, after) == (1, True)
        passed.append("SET_CHECKED flips a transparent radio and verifies")
        page = browser.observe(screenshot=False)
        economy = next(a for a in page["actions"] if a["label"] == "Set Economy (Class)")
        result = browser.act(economy, page)
        assert result.get("already_set") is True
        assert browser.evaluate("window.roleClicks || 0") == 0
        passed.append("already-set checkable is a verified no-op")
        page = browser.observe(screenshot=False)
        day = next(a for a in page["actions"] if a["label"] == "13")
        browser.act(day, page)
        assert browser.evaluate("window.pickedDay") == "13"
        passed.append("calendar day click selects the day")

        browser.call("Page.navigate", url="data:text/html," + quote((FIXTURES / "widgets.html").read_text()))
        browser.wait_until_ready()
        page = browser.observe(screenshot=False)
        city = next(a for a in page["actions"] if a.get("kind") == "fill" and a.get("label") == "Destination")
        assert city.get("role") == "combobox"
        result = browser.act(city, page, text="Lisbon")
        assert result.get("commit") == "option-click", result
        assert result.get("picked") == "Lisbon, Portugal", result
        assert result.get("committed") is True, result
        assert browser.evaluate("document.querySelector('#city').value") == "Lisbon, Portugal"
        passed.append("combobox typing commits the best-matching option and survives blur")
        page = browser.observe(screenshot=False)
        city = next(a for a in page["actions"] if a.get("kind") == "fill" and a.get("label") == "Destination")
        result = browser.act(city, page, text="Xyz")
        assert result.get("commit") == "keep-typed", result
        assert result.get("picked") is None, result
        assert result.get("committed") is True, result
        assert result.get("retried") is False, result
        assert browser.evaluate("document.querySelector('#city').value") == "Xyz"
        passed.append("unmatched typing keeps its value without clicking a wrong option")
        browser.evaluate("document.querySelector('#city').value=''; document.querySelector('#city').focus()")
        browser.call("Input.insertText", text="L")
        shown = browser.evaluate(combo_options_expression())
        assert any(option.get("secondary") == "City in Portugal" for option in shown), shown
        assert all("\n" not in (option.get("label") or "") for option in shown), shown
        passed.append("listbox options expose first-line labels and secondary text")

        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *_args):
                pass

        os.environ["JEV_ALLOW_LOCAL"] = "1"
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(FIXTURES)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            target = f"http://127.0.0.1:{server.server_port}/shadow.html"
            result = browser.navigate(target)
            assert result == {"executed": "navigate", "url": target}, result
            page = browser.observe(screenshot=False)
            assert page["url"].startswith("http://127.0.0.1:"), page["url"]
            assert "Inside shadow" in [a["label"] for a in page["actions"]]
            passed.append("navigate round-trips a local page and records success")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            os.environ.pop("JEV_ALLOW_LOCAL", None)

        browser.call("Page.navigate", url="data:text/html," + quote("<title>Calm</title><p>Nothing here.</p>"))
        browser.wait_until_ready()
        page = browser.observe(screenshot=False)
        assert page.get("status") == 200, page.get("status")
        assert page.get("captcha_reason") is None, page.get("captcha_reason")
        browser.call("Page.navigate", url="data:text/html," + quote(
            "<title>Wall</title><p>Our systems have detected unusual traffic.</p>"
            '<iframe src="https://recaptcha.invalid/anchor" width="300" height="80"></iframe>'
        ))
        browser.wait_until_ready()
        page = browser.observe(screenshot=False)
        assert (page.get("captcha_reason") or "").startswith("challenge "), page.get("captcha_reason")
        passed.append("captcha text and challenge iframe flag the page")

        browser.call("Page.navigate", url="data:text/html," + quote("<title>First</title><p>first</p>"))
        browser.wait_until_ready()
        browser.call("Page.navigate", url="data:text/html," + quote("<title>Second</title><p>second</p>"))
        browser.wait_until_ready()
        browser.go_back()
        browser.wait_until_ready()
        page = browser.observe(screenshot=False)
        assert page["title"] == "First", page["title"]
        passed.append("go_back returns to the previous document")

        browser.call("Page.navigate", url="data:text/html," + quote((FIXTURES / "semantics.html").read_text()))
        browser.wait_until_ready()
        page = browser.observe(screenshot=False)
        actions = page["actions"]
        assert any(a.get("label") == "Close dialog" for a in actions)
        assert any(a.get("label") == "Search flights" for a in actions)
        assert any(a.get("kind") == "fill" and a.get("label", "").startswith("Enter the promo code") for a in actions)
        assert any(a.get("label") == "lisbon portugal" for a in actions)
        near = next(a for a in actions if "Passenger options" in a.get("label", ""))
        assert near.get("generic") is True
        passed.append("title, svg title, describedby, href tail, and nearby text label controls")
        dec = next(a for a in actions if a.get("label") == "Decrease adults")
        inc = next(a for a in actions if a.get("label") == "Increase adults")
        assert dec.get("value") == "2", dec
        assert inc.get("value") == "2", inc
        assert any(a.get("value") == "3" for a in actions)
        passed.append("stepper buttons carry the shared count value")
        off = [a for a in actions if a.get("offscreen")]
        assert len(off) == 5, [a.get("label") for a in off]
        assert any(a.get("label") == "Continue reading" for a in off)
        passed.append("offscreen pagers are indexed, capped, and rel=next counts")
        more = next(a for a in actions if a.get("label") == "More" and a.get("offscreen"))
        browser.act(more, page)
        assert browser.evaluate("window.moreClicked") is True
        passed.append("offscreen pager click scrolls into view first")
    finally:
        browser.close()
    print("\n".join(passed))
    print(f"PASS: {len(passed)} browser guard checks; no model calls")


if __name__ == "__main__":
    main()
