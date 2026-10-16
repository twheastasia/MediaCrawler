"""Public WeChat/Sogou document extraction; never serialize browser session state.

Selectors and timestamp formats researched from OpenCLI's weixin adapters:
https://github.com/jackwener/OpenCLI/tree/main/clis/weixin (Apache-2.0).
This implementation preserves article HTML before deriving text/Markdown.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from lxml import html as lh

from collection.runtime import AccessRestricted
from .exception import ArticleUnavailable, ArticleSkipped, ParseError
from model.m_weixin import WeixinArticleRaw, WeixinContent, WeixinSearchResult

PARSER_VERSION = "weixin-2"
SENSITIVE = re.compile(r"cookie|session|password|passwd|authorization|(?:^|_)token$|^key$|pass_ticket|uin|ticket|credential", re.I)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def public_url(value, base="https://mp.weixin.qq.com/"):
    """Only public HTTP(S) references; remove known session-bearing query keys."""
    # DOM URLs are already decoded. HTML's semicolon-less entities would turn
    # &timestamp into ×tamp (and corrupt other ordinary query parameter names).
    value = (value or "").strip().replace("&amp;", "&")
    parsed = urlsplit(urljoin(base, value))
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        return ""
    pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not SENSITIVE.search(k)]
    return urlunsplit(("https", parsed.netloc, parsed.path, urlencode(pairs), ""))


def article_url(value):
    value = value.strip().strip('"\'“”‘’<>「」').replace("&amp;", "&")
    value = re.sub(r"\\+([:/&?=#%])", r"\1", value)
    if value.startswith("mp.weixin.qq.com/"):
        value = "https://" + value
    value = public_url(value)
    p = urlsplit(value)
    if p.hostname != "mp.weixin.qq.com" or p.port not in (None, 443) or not re.fullmatch(r"/s(?:/[^/]+)?", p.path):
        raise ValueError("invalid_article_url")
    pairs = [(k, v) for k, v in parse_qsl(p.query) if k in {"__biz", "mid", "idx", "sn", "src", "timestamp", "ver", "signature", "new"}]
    if p.path == "/s" and not ({"__biz", "mid"} <= dict(pairs).keys() or dict(pairs).get("signature")):
        raise ValueError("incomplete_article_url")
    return urlunsplit(("https", "mp.weixin.qq.com", p.path, urlencode(sorted(pairs)), ""))


def candidate_url(value):
    safe = public_url(value, "https://weixin.sogou.com/")
    p = urlsplit(safe)
    if p.hostname == "mp.weixin.qq.com":
        return article_url(safe)
    if p.hostname == "weixin.sogou.com" and p.path == "/link" and p.port in (None, 443):
        return safe
    raise ValueError("unsupported_discovery_url")


def discovery_navigation_url(value):
    """Transient Sogou routing token: use in memory, never persist this URL."""
    canonical = candidate_url(value)
    if urlsplit(canonical).hostname != "weixin.sogou.com":
        return canonical
    raw = urlsplit(urljoin("https://weixin.sogou.com/", value.replace("&amp;", "&")))
    # Only the public bridge's routing parameters may accompany its token.
    pairs = [(k, v) for k, v in parse_qsl(raw.query, keep_blank_values=True)
             if k in {"url", "type", "query", "token"}]
    return urlunsplit(("https", "weixin.sogou.com", "/link", urlencode(pairs), ""))


def search_url(keyword, page):
    return "https://weixin.sogou.com/weixin?" + urlencode({"query": keyword, "type": 2, "page": page, "ie": "utf8"})


def access_check(text, page_url="", verify=False):
    if verify or re.search(r"验证码|安全验证|异常访问|访问过于频繁|完成验证后|环境异常|访问受限|操作频繁|captcha|unusual traffic|too (?:many requests|frequent)|access denied|(?:complete|security).{0,25}verification|verify.{0,25}(?:human|identity)", text, re.I):
        raise AccessRestricted("verification_required")
    if "secitptpage/verify" in page_url:
        raise AccessRestricted("verification_required")
    if re.search(r"内容已被发布者删除|该内容已被发布者删除|此内容因违规无法查看|该内容无法查看|链接已过期|链接已失效|参数错误|页面不存在|内容已删除|此内容已被删除|content (?:has been |was |is )?(?:deleted|unavailable)|content.{0,30}not available|link.{0,20}(?:expired|invalid)", text, re.I):
        raise ArticleUnavailable("article_unavailable")


def sanitize_html(markup):
    root = lh.fragment_fromstring(markup or "", create_parent="div")
    for element in list(root.iter()):
        if not isinstance(element.tag, str):
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
            continue
        if element.tag.lower() in {"script", "iframe", "input", "form"}:
            # Embedded public media references are collected separately before this.
            element.drop_tree()
            continue
        for key in list(element.attrib):
            if key.lower().startswith("on") or SENSITIVE.search(key) or key.lower() == "srcset":
                del element.attrib[key]
            elif key.lower() in {"src", "href", "data-src", "poster", "data-url", "data-original"}:
                safe = public_url(element.attrib[key])
                if safe:
                    element.attrib[key] = safe
                else:
                    del element.attrib[key]
    return (root.text or "") + "".join(lh.tostring(child, encoding="unicode") for child in root)


def markdown_of(markup):
    root = lh.fragment_fromstring(markup, create_parent="div")

    def render(node, depth=0):
        tag = node.tag.lower() if isinstance(node.tag, str) else ""
        if tag in {"script", "style"}:
            return ""
        inner = (node.text or "") + "".join(render(c, depth + 1) + (c.tail or "") for c in node)
        if tag == "img":
            src = node.get("data-src") or node.get("src")
            return f"![{node.get('alt', '')}]({src})" if src else ""
        if tag == "a" and node.get("href"):
            return f"[{inner}]({node.get('href')})"
        if tag == "br":
            return "\n"
        if tag == "pre":
            return "\n\n```\n" + node.text_content().strip("\n") + "\n```\n\n"
        if tag == "code":
            return "`" + inner + "`"
        if tag in {"strong", "b"}:
            return "**" + inner + "**"
        if tag in {"em", "i"}:
            return "*" + inner + "*"
        if re.fullmatch(r"h[1-6]", tag):
            return "\n\n" + "#" * int(tag[1]) + " " + inner.strip() + "\n\n"
        if tag == "li":
            return "\n- " + inner.strip()
        if tag == "blockquote":
            return "\n\n" + "\n".join("> " + line for line in inner.strip().splitlines()) + "\n\n"
        if tag == "table":
            # Markdown accepts inline HTML; retain merged cells and complex tables.
            return "\n\n" + lh.tostring(node, encoding="unicode", with_tail=False) + "\n\n"
        if tag == "tr":
            return "\n| " + " | ".join(c.text_content().strip() for c in node if c.tag in {"td", "th"}) + " |\n"
        if tag in {"p", "div", "section", "ul", "ol", "table"}:
            return "\n\n" + inner.strip() + "\n\n"
        return inner

    return re.sub(r"\n{3,}", "\n\n", render(root)).strip()


def publish_time(text, timestamp):
    if re.fullmatch(r"\d{10}|\d{13}", str(timestamp or "")):
        number = int(timestamp)
        return number // 1000 if len(str(number)) == 13 else number, "second"
    text = (text or "").strip()
    for pattern, precision in (("%Y-%m-%d %H:%M:%S", "second"), ("%Y-%m-%d %H:%M", "minute")):
        try:
            return int(datetime.strptime(text, pattern).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()), precision
        except ValueError:
            pass
    # Date-only or relative text stays raw; do not invent an exact timestamp.
    return None, "day" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else "unknown"


def article_identity(url, metadata):
    query = dict(parse_qsl(urlsplit(url).query))
    biz = metadata.get("biz") or query.get("__biz")
    mid = metadata.get("mid") or query.get("mid")
    idx = metadata.get("idx") or query.get("idx")
    if biz and mid and idx:
        return "wx_" + digest(f"{biz}:{mid}:{idx}"), biz
    return "url_" + digest(url), biz or None


def parse_article(snapshot):
    check_article_page(snapshot)
    try:
        url = article_url(snapshot.get("canonical") or snapshot["url"])
    except ValueError:
        # A discovery bridge can fail to deliver an article despite HTTP 200.
        # Keep this candidate as unavailable and proceed to other discoveries.
        if urlsplit(snapshot.get("url", "")).hostname == "weixin.sogou.com":
            raise ArticleUnavailable("discovery_redirect_unavailable") from None
        raise ParseError("article_url_structure_changed") from None
    title = snapshot.get("title", "").strip()
    markup = sanitize_html(snapshot.get("content_html", ""))
    if not title or not markup.strip():
        raise ParseError("missing_article_content")
    metadata = {key: str(snapshot.get(key) or "") for key in ("account_name", "byline", "publish_text", "timestamp", "biz", "mid", "idx")}
    content_id, author_id = article_identity(url, metadata)
    root = lh.fragment_fromstring(markup, create_parent="div")
    resources = []
    seen = set()
    public_media = list(snapshot.get("media_refs", []))
    for img in root.xpath(".//img"):
        public_media.append({"type": "image", "url": img.get("data-src") or img.get("src"), "alt": img.get("alt", "")})
    for ref in public_media:
        safe = public_url(ref.get("url")) if ref.get("url") else ""
        if safe and safe not in seen:
            seen.add(safe)
            kind = ref.get("type", "attachment")
            if kind == "link" and (ref.get("download") or re.search(r"\.(?:pdf|docx?|xlsx?|pptx?|zip|rar|7z|mp[34]|wav)$", urlsplit(safe).path, re.I)):
                kind = "attachment"
            resources.append({"type": kind, "url": safe, "stem": digest(safe)[:24]})
    stamp, precision = publish_time(metadata["publish_text"], metadata["timestamp"])
    raw = WeixinArticleRaw(
        article_html=markup, article_markdown=markdown_of(markup),
        metadata_raw=metadata, time_precision=precision, media_refs=resources,
        parser_version=PARSER_VERSION,
    )
    return WeixinContent(
        id=content_id, author_id=author_id, title=title, body=root.text_content(),
        url=url, time=stamp, raw=raw.model_dump(),
    ).model_dump()


def check_article_page(snapshot):
    """Preserve real barriers; skip account/content failures without following links."""
    access_check(snapshot.get("page_text", ""), snapshot.get("url", ""), snapshot.get("verify", False))
    if "该公众号已迁移" in snapshot.get("page_text", ""):
        raise ArticleSkipped("account_migrated")
    if re.search(r"(?:此|该)?(?:公众)?账号已被(?:屏蔽|封禁)|(?:此|该)账号已注销", snapshot.get("page_text", "")):
        raise ArticleSkipped("account_blocked")


def parse_search(snapshot):
    access_check(snapshot.get("page_text", ""), snapshot.get("url", ""), snapshot.get("verify", False))
    cards = snapshot.get("cards", [])
    if not cards:
        if snapshot.get("empty"):
            return []
        raise ParseError("search_structure_changed")
    result = []
    for rank, card in enumerate(cards, 1):
        if not card.get("title") or not card.get("url"):
            raise ParseError("search_card_incomplete")
        try:
            url = candidate_url(card["url"])
        except ValueError:
            raise ParseError("search_link_changed") from None
        result.append(WeixinSearchResult(
            url=url, title=card["title"], summary=card.get("summary", ""),
            publish_text=card.get("publish_text", ""), rank_in_page=rank,
            card_html=sanitize_html(card.get("html", "")),
        ).model_dump())
    return result


# Selectively read public article fields; never return document HTML or scripts.
ARTICLE_SNAPSHOT = r"""() => {
  const text = (...selectors) => selectors.map(s=>document.querySelector(s)?.textContent?.trim()).find(Boolean)||'';
  const content = document.querySelector('#js_content');
  const source = [...document.scripts].map(s=>s.textContent||'').join('\n');
  const field = name => {
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const m = source.match(new RegExp('(?:\\bvar\\s+|[,{;\\s])'+escaped+'\\s*[:=]\\s*(?:JsDecode\\()?(["\x27])([^"\x27]*)\\1'));
    if (m) return m[2];
    const numeric = source.match(new RegExp('(?:\\bvar\\s+|[,{;\\s])'+escaped+'\\s*[:=]\\s*(\\d+)(?=[\\s,;}])'));
    return numeric ? numeric[1] : '';
  };
  const stamp = source.match(/(?:create_time|\bct)\s*[:=]\s*(?:JsDecode\()?['"]?(\d{10}(?:\d{3})?)(?!\d)/);
  const media = content ? [...content.querySelectorAll('video,audio,iframe,mpvoice,mpvideo,a[href]')].map(e=>({
    type: e.tagName==='A'?'link':e.tagName.toLowerCase(),
    download: e.hasAttribute('download'),
    url: e.getAttribute('src')||e.getAttribute('data-src')||e.getAttribute('href')||''
  })).filter(x=>x.url) : [];
  return {url:location.href, canonical:document.querySelector('meta[property="og:url"]')?.content||'',
    page_text:content?'':(document.body?.innerText||''),
    migration_url:document.querySelector('a#js_access_msg')?.getAttribute('href')||'',
    verify:!!document.querySelector('#js_verify')||location.href.includes('secitptpage/verify'),
    title:text('#activity-name','#js_text_title','.rich_media_title'),
    account_name:text('#js_name','.wx_follow_nickname','#profileBt .profile_nickname','.rich_media_meta_nickname'),
    byline:text('#js_author_name','.rich_media_meta_text'),publish_text:text('#publish_time'),
    timestamp:stamp?stamp[1]:'',biz:field('biz')||field('__biz'),mid:field('mid')||field('appmsgid'),idx:field('idx'),
    content_html:content?.innerHTML||'',media_refs:media};
}"""

SEARCH_SNAPSHOT = r"""() => ({url:location.href,
  page_text:document.querySelector('.news-list')?'':(document.body?.innerText||''),
  verify:!!document.querySelector('#seccodeForm,#seccodeInput') ||
    (!document.querySelector('.news-list li') && /验证码|安全验证|异常访问|访问过于频繁|captcha|verification|too frequent/i.test(document.body?.innerText||'')),
  empty:/没有找到相关的微信文章|未找到相关|暂无相关|没有找到/.test(document.body?.innerText||''),
  cards:[...document.querySelectorAll('.news-list li')].map(e=>({
    title:e.querySelector('h3 a')?.textContent?.trim()||'',url:e.querySelector('h3 a')?.href||'',
    summary:e.querySelector('p.txt-info')?.textContent||'',publish_text:e.querySelector('.s-p .s2')?.textContent||'',
    html:e.outerHTML}))
})"""
