from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

ResearchWebSearchType = Literal["auto", "fast", "deep"]
ResearchWebSearchLivecrawl = Literal["fallback", "preferred"]


class ResearchWebSearchRequest(TypedDict):
    query: str
    sessionID: str
    numResults: NotRequired[int]
    type: NotRequired[ResearchWebSearchType]
    livecrawl: NotRequired[ResearchWebSearchLivecrawl]
    contextMaxCharacters: NotRequired[int]


class ResearchWebSearchQuery(TypedDict):
    query: str
    numResults: int
    type: ResearchWebSearchType
    livecrawl: ResearchWebSearchLivecrawl
    sessionID: str
    contextMaxCharacters: int | None


class ResearchProviderMetadata(TypedDict, total=False):
    id: Any
    author: Any
    publishedDate: Any
    image: Any
    favicon: Any
    highlightScores: Any


class ResearchWebSearchResult(TypedDict):
    title: str
    url: str
    contentExcerpt: str
    providerMetadata: ResearchProviderMetadata


class ResearchWebSearchProvider(TypedDict):
    name: str
    requestId: Any
    searchType: Any
    cached: bool


class ResearchProviderUsage(TypedDict):
    name: str
    cached: bool
    costDollars: Any


class ResearchCreditUsage(TypedDict):
    charged: int
    used: int
    remaining: int
    quota: int


class ResearchWebSearchUsage(TypedDict):
    provider: ResearchProviderUsage
    credits: ResearchCreditUsage | None


class ResearchWebSearchResponse(TypedDict):
    query: ResearchWebSearchQuery
    results: list[ResearchWebSearchResult]
    provider: ResearchWebSearchProvider
    usage: ResearchWebSearchUsage


class ResearchGatewayErrorBody(TypedDict, total=False):
    type: str
    code: str
    message: str
    details: Any
