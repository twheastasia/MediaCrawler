"""Public WeChat article models shared by extraction and persistence."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class WeixinMediaRef(BaseModel):
    type: str
    url: str
    stem: str


class WeixinArticleRaw(BaseModel):
    # Keep future public fields rather than dropping original acquisition data.
    model_config = ConfigDict(extra="allow")

    article_html: str
    article_markdown: str
    metadata_raw: dict[str, Any]
    time_precision: str
    media_refs: list[WeixinMediaRef] = Field(default_factory=list)
    parser_version: str


class WeixinContent(BaseModel):
    type: Literal["article"] = "article"
    id: str
    author_id: str | None = None
    title: str
    body: str
    url: str
    time: int | None = None
    raw: dict[str, Any]


class WeixinAuthor(BaseModel):
    author_id: str
    name: str | None = None
    home_url: str | None = None
    raw: dict[str, Any]


class WeixinSearchResult(BaseModel):
    url: str
    title: str
    summary: str | None = ""
    publish_text: str | None = ""
    rank_in_page: int
    card_html: str
