"""app/gallery/zorcam_html.py: markup that always escapes."""
import json

import pytest

from zorcam_html import Markup, attrs, cat, esc, h, js_json, trusted

EVIL = '<script>alert("x")</script> \'q\' & </b>'


def test_text_is_escaped_and_markup_is_not_escaped_twice():
    assert esc(EVIL) == "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &#x27;q&#x27; &amp; &lt;/b&gt;"
    assert esc(5) == "5" and esc(0) == "0" and esc("") == ""
    ready = trusted("<b>x</b>")
    assert esc(ready) is ready and isinstance(esc(EVIL), Markup)
    assert h("p", ready, EVIL) == "<p><b>x</b>" + esc(EVIL) + "</p>"
    assert h("p", h("b", EVIL)) == "<p><b>" + esc(EVIL) + "</b></p>"  # a nested element stays an element


def test_attribute_values_cannot_leave_the_attribute():
    assert h("a", "t", href='x" onmouseover="alert(1)') == '<a href="x&quot; onmouseover=&quot;alert(1)">t</a>'
    assert h("input", value=EVIL).count("<") == 1 and '"' not in h("input", value=EVIL)[len('<input value="'):-2]
    assert attrs(title="'") == ' title="&#x27;"'


def test_attribute_names_values_and_empty_ones():
    assert attrs(class_="a b", data_cam="front", aria_label="x", hidden=True, disabled=False, title=None, n=0,
                 http_equiv="refresh") == ' class="a b" data-cam="front" aria-label="x" hidden n="0" http-equiv="refresh"'
    assert attrs(**{"data-x": "1"}) == ' data-x="1"'
    assert h("input", type="checkbox", checked=True) == '<input type="checkbox" checked>'  # no closing tag
    assert h("br") == "<br>" and h("div") == "<div></div>"


def test_content_is_flattened_and_none_is_skipped():
    rows = (h("li", n) for n in (1, "<2>"))
    assert h("ul", rows, None, ["x", ("y",)]) == "<ul><li>1</li><li>&lt;2&gt;</li>xy</ul>"
    assert cat("a<", trusted("<i>"), None, [h("b", "c")]) == "a&lt;<i><b>c</b>"
    assert cat() == "" and not cat(None, "")


def test_json_for_a_script_block_cannot_close_it():
    out = js_json({"t": "</script><script>alert(1)</script>", "ru": "\u0442\u0435\u0441\u0442"})
    assert "<" not in out and json.loads(out) == {"t": "</script><script>alert(1)</script>", "ru": "\u0442\u0435\u0441\u0442"}


@pytest.mark.parametrize("bad", ["<img src=x onerror=alert(1)>", "\"><svg onload=alert(1)>", "'-alert(1)-'"])
def test_a_page_element_with_hostile_data_has_no_foreign_tag(bad):
    out = h("div", h("span", bad, title=bad), class_="row", data_x=bad)
    assert out.count("<") == 4 and out.count(">") == 4  # <div> <span> </span> </div>: no tag from the data
    assert out.count('"') == 6 and "'" not in out  # the quotes of three attributes only


def test_the_gallery_code_escapes_only_through_the_helper():
    """The reason for zorcam_html: no hand-written html.escape in an f-string (one forgotten is an XSS) and no CSS or
    JavaScript in the code (they are files in static/)."""
    import os
    from conftest import SRC
    with open(os.path.join(SRC, "gallery", "app.py"), encoding="utf-8") as f:
        code = f.read()
    assert "html.escape" not in code and "import html" not in code
    assert "<style" not in code and "</script>" not in code.replace('f"<script>{code}</script>"', "")
