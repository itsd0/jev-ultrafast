"""Local-browser freshness/execution regressions. No model calls or external websites."""

import json
from pathlib import Path
from urllib.parse import quote

from jev_ultrafast.browser import Browser, StalePage

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

        mutations = {
            "visible context": "document.querySelector('#context').textContent='Cart total: $100'",
            "accessible label": "document.querySelector('#target').setAttribute('aria-label','Delete account')",
            "field property": "document.querySelector('#field').value='London'",
            "checkbox property": "document.querySelector('#toggle').checked=true",
            "disabled target": "document.querySelector('#target').disabled=true",
            "read-only field": "document.querySelector('#field').readOnly=true",
            "hidden target": "document.querySelector('#target').style.display='none'",
            "replaced node": "document.querySelector('#target').outerHTML=document.querySelector('#target').outerHTML",
            "dropdown option": "document.querySelector('select').options[1].text='Coastal'",
        }
        for label, expression in mutations.items():
            browser.evaluate("document.querySelector('#target').style.display='block'; "
                             "document.querySelector('#target').disabled=false")
            page = browser.observe(screenshot=False)
            browser.evaluate(expression)
            assert not browser.fresh(page), label
            passed.append(label + " invalidates")

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
        assert not browser.fresh(page)
        passed.append("click guard accepts unrelated visible updates; terminal guard rejects them")
        for label, expression in {
            "nearby price": "document.querySelector('#price').textContent='Total $100'",
            "form value": "document.querySelector('#query').value='changed'",
            "form toggle": "document.querySelector('#check').checked=true",
            "target replacement": "document.querySelector('#buy').outerHTML=document.querySelector('#buy').outerHTML",
        }.items():
            page = browser.observe(screenshot=False)
            buy = next(a for a in page["actions"] if a["label"] == "Buy")
            browser.evaluate(expression)
            assert not browser.fresh(page, buy), label
            passed.append(label + " invalidates action-specific guard")

        page = browser.observe(screenshot=False)
        actions = page["actions"]
        for role in ("checkbox", "radio"):
            assert {a["kind"] for a in actions if a.get("role") == role} == {"click"}
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
    finally:
        browser.close()
    print("\n".join(passed))
    print(f"PASS: {len(passed)} browser guard checks; no model calls")


if __name__ == "__main__":
    main()
