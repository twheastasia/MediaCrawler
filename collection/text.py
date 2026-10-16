"""Extract HTML text without interpreting numeric/JSON-shaped content as JSON."""
from lxml import html


def html_text(value):
    # Parsel auto-detects valid JSON even with type='html'; xpath then fails on
    # ordinary comments such as 123, null or [1,2]. Raw content stays untouched.
    markup='' if value is None else str(value)
    return html.fragment_fromstring(markup,create_parent='div').text_content()
