from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

ResearchWebSearchType = Literal[
    "auto",
    "fast",
    "instant",
    "deep-lite",
    "deep",
    "deep-reasoning",
    "neural",
]
ResearchWebSearchLivecrawl = Literal["fallback", "preferred"]
ResearchWebContentType = Literal["highlights", "text", "summary"]
ResearchWebSearchCategory = Literal[
    "company",
    "people",
    "research paper",
    "news",
    "personal site",
    "financial report",
]


class ResearchWebSearchRequest(TypedDict):
    query: str
    sessionID: str
    numResults: NotRequired[int]
    type: NotRequired[ResearchWebSearchType]
    category: NotRequired[ResearchWebSearchCategory]
    includeDomains: NotRequired[list[str]]
    excludeDomains: NotRequired[list[str]]
    startPublishedDate: NotRequired[str]
    endPublishedDate: NotRequired[str]
    maxAgeHours: NotRequired[int]
    additionalQueries: NotRequired[list[str]]
    contentType: NotRequired[ResearchWebContentType]
    livecrawl: NotRequired[ResearchWebSearchLivecrawl]
    contextMaxCharacters: NotRequired[int]


class ResearchWebSearchQuery(TypedDict):
    query: str
    numResults: int
    type: ResearchWebSearchType
    category: str | None
    includeDomains: list[str] | None
    excludeDomains: list[str] | None
    startPublishedDate: Any
    endPublishedDate: Any
    maxAgeHours: int | None
    additionalQueries: list[str] | None
    contentType: ResearchWebContentType
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


class ResearchWebFetchRequest(TypedDict):
    urls: list[str]
    sessionID: str
    query: NotRequired[str]
    contentType: NotRequired[ResearchWebContentType]
    livecrawl: NotRequired[ResearchWebSearchLivecrawl]
    maxAgeHours: NotRequired[int]
    subpages: NotRequired[int]
    subpageTarget: NotRequired[list[str]]
    contextMaxCharacters: NotRequired[int]


class ResearchWebFetchQuery(TypedDict):
    urls: list[str]
    query: str
    contentType: ResearchWebContentType
    livecrawl: ResearchWebSearchLivecrawl
    maxAgeHours: int | None
    subpages: int | None
    subpageTarget: list[str] | None
    sessionID: str
    contextMaxCharacters: int | None


class ResearchWebFetchResponse(TypedDict):
    query: ResearchWebFetchQuery
    results: list[ResearchWebSearchResult]
    statuses: list[dict[str, Any]]
    provider: ResearchWebSearchProvider
    usage: ResearchWebSearchUsage


class ResearchCryptoSentimentRequest(TypedDict):
    sessionID: str


class ResearchCryptoSentimentRow(TypedDict):
    value: int
    classification: str
    timestamp: str
    timeUntilUpdate: Any


class ResearchCryptoSentimentResponse(TypedDict):
    query: dict[str, Any]
    results: list[ResearchCryptoSentimentRow]
    provider: dict[str, Any]
    usage: ResearchWebSearchUsage
    context: dict[str, Any]


class ResearchSocialXSearchRequest(TypedDict):
    query: str
    sessionID: str
    allowedXHandles: NotRequired[list[str]]
    excludedXHandles: NotRequired[list[str]]
    fromDate: NotRequired[str]
    toDate: NotRequired[str]


class ResearchSocialXSearchResponse(TypedDict):
    query: dict[str, Any]
    result: dict[str, Any]
    provider: dict[str, Any]
    usage: ResearchWebSearchUsage
    context: dict[str, Any]


class ResearchGatewayErrorBody(TypedDict, total=False):
    type: str
    code: str
    message: str
    details: Any
