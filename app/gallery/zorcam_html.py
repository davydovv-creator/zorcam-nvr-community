"""Markup for the gallery pages that always escapes: text and attribute values go through html.escape here and only
here, trusted HTML is passed explicitly (Markup, trusted()). A forgotten html.escape in an f-string cannot happen when the
element is built with h().

    h("a", camera["name"], href=url, class_="pill", hidden=True)   # <a href="…" class="pill" hidden>…</a>
    h("div", h("span", name), " ", trusted(ICON), class_="row")
"""
import html
import json

VOID = frozenset(("area", "base", "br", "col", "hr", "img", "input", "link", "meta", "source", "track", "wbr"))


class Markup(str):
    """HTML that is trusted: h(), cat() and esc() do not escape it again. Join pieces with cat(), not with +: a plain
    str + Markup is a plain str again."""

    __slots__ = ()


def trusted(text):
    """Mark ready HTML (a constant, an icon, a template) as trusted. Never for data: use esc() or h() for it."""
    return Markup(text)


def esc(value):
    """value as HTML text or attribute value: &, <, >, both quotes escaped; Markup stays as it is."""
    if isinstance(value, Markup):
        return value
    return Markup(html.escape(str(value), quote=True))


def cat(*parts):
    """Parts one after another: text is escaped, Markup stays, lists (also generators) are flattened, None is skipped."""
    out = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, (list, tuple)) or (hasattr(part, "__next__") and not isinstance(part, str)):
            out.append(cat(*part))
        else:
            out.append(esc(part))
    return Markup("".join(out))


def attrs(**kw):
    """Attributes as ' name="value"' for each: class_ is class, data_cam is data-cam (underscore -> hyphen, a trailing
    underscore is dropped); True is a bare attribute (hidden), False and None are left out."""
    out = []
    for name, value in kw.items():
        if value is None or value is False:
            continue
        name = name.rstrip("_").replace("_", "-")
        out.append(f" {name}" if value is True else f' {name}="{esc(value)}"')
    return Markup("".join(out))


def h(tag, *content, **kw):
    """An element: h("a", text, href=url). Content is escaped (Markup and nested h() are not), attributes too."""
    if tag in VOID:
        return Markup(f"<{tag}{attrs(**kw)}>")
    return Markup(f"<{tag}{attrs(**kw)}>{cat(*content)}</{tag}>")


def js_json(value):
    """JSON for a <script> block (window.T = …): "<" is escaped, so that "</script>" in a string cannot close it."""
    return Markup(json.dumps(value, ensure_ascii=False).replace("<", "\\u003c"))
