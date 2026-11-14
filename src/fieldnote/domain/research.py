"""Deterministic research boundaries shared by validation and execution.

These deliberately conservative rules are application policy, not a classifier
claim. Ambiguous factual briefs stay researched rather than guessing that a
named product or a number is timeless.
"""

import re

_FRESH = re.compile(
    r"\b(latest|current|currently|today|yesterday|tomorrow|recent|recently|"
    r"news|this\s+(?:week|month|year)|up[- ]to[- ]date|pricing|prices?|costs?|"
    r"rates?|statistics?|benchmarks?|versions?|releases?|regulations?|laws?|"
    r"legislation|legal|medical|dosage|clinical|investment|financial|tax|"
    r"güncel\w*|bugün|dün|yarın|en\s+son|fiyat\w*|maliyet\w*|istatistik\w*|"
    r"sürüm\w*|yasa\w*|mevzuat|hukuk\w*|tıbbi|ilaç\w*|doz\w*|yatırım\w*|finans\w*|vergi\w*|"
    r"actual(?:es|mente)?|hoy|ayer|mañana|reciente\w*|noticias|precio\w*|costo\w*|coste\w*|"
    r"estadístic\w*|versi[oó]n\w*|lanzamiento\w*|regulaci[oó]n\w*|leyes?|legislaci[oó]n|"
    r"jurídic\w*|médic\w*|dosis|inversi[oó]n|financier\w*|impuesto\w*|"
    r"atua(?:l\w*|is)|hoje|ontem|amanhã|notícias|preço\w*|custo\w*|vers[aã]o|lançamento\w*|"
    r"regulament\w*|leis?|legislaç\w*|investimento\w*|financeir\w*|"
    r"aktuell\w*|heute|gestern|morgen|neueste\w*|nachrichten|preis\w*|kosten\w*|statistik\w*|"
    r"veröffentlich\w*|verordnung\w*|gesetz\w*|rechtlich\w*|medizin\w*|medikament\w*|dosier\w*|investition\w*|finanz\w*|steuer\w*)\b",
    re.IGNORECASE,
)
_SOURCES = re.compile(
    r"\b(sources?|citations?|cite|references?|evidence|verify|fact[- ]check|"
    r"official\s+(?:docs?|documentation|information)|research|"
    r"kaynak\w*|atıf\w*|kanıt\w*|doğrula\w*|araştır\w*|"
    r"fuente\w*|cita\w*|referencia\w*|evidencia\w*|verific\w*|investigaci[oó]n|"
    r"fonte\w*|citaç\w*|referência\w*|evidência\w*|pesquis\w*|"
    r"quelle\w*|zitat\w*|beleg\w*|nachweis\w*|überprüf\w*|recherch\w*)\b",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"\d")
_NAMED = re.compile(
    r"\b(?:[A-Z]{2,}[A-Za-z0-9]*|[A-Z][a-z]+[A-Z][A-Za-z0-9]*|"
    r"iPhone|iPad|macOS|Windows|Linux|Samsung|Google|Microsoft|Anthropic|"
    r"Gemini|Claude|Tavily|Treg|OpenRouter|Docker|Python|JavaScript|"
    r"PostgreSQL|FastAPI|LangGraph)\b"
)
_CREATIVE = re.compile(
    r"\b(poem|poetry|fiction|fictional|fantasy|fable|fairy\s+tale|"
    r"bedtime\s+story|creative\s+story|short\s+story|imaginary|invented|"
    r"şiir\w*|öykü\w*|hikâye\w*|hikaye\w*|masal\w*|kurgu\w*|hayali|uydurma|"
    r"poema\w*|poesía|ficción|fábula\w*|cuento\w*|imaginari\w*|poesia|ficção|conto\w*|"
    r"gedicht\w*|poesie|fiktion\w*|fiktiv\w*|märchen|kurzgeschichte\w*|fantasie\w*|erfunden\w*)\b",
    re.IGNORECASE,
)
_GENERAL = re.compile(
    r"^(?:explain|describe|introduce|understand|understanding|introduction\s+to|"
    r"how|why|what\s+is|what\s+are|the\s+basics\s+of|a\s+beginner['’]?s?\s+guide\s+to|"
    r"açıkla|anlat|nasıl|neden|qué\s+es|qué\s+son|cómo|por\s+qué|explica\w*|describe\w*|"
    r"o\s+que\s+é|como|por\s+que|explique|descreva|wie|warum|was\s+ist|was\s+sind|erkläre\w*|beschreibe\w*)\b",
    re.IGNORECASE,
)


def factual_research_reason(topic: str, instructions: str = "") -> str | None:
    """Return the first non-overridable research reason, if one is explicit."""
    text = f"{topic} {instructions}"
    if _SOURCES.search(text):
        return "explicit_sources"
    if _FRESH.search(text):
        return "current_or_sensitive_facts"
    if _NUMBER.search(text):
        return "numeric_claims"
    # Skip the initial word to avoid treating a sentence's initial capital as a
    # named entity. Known brands/acronyms remain checked in the whole brief.
    if _NAMED.search(text):
        return "named_product_or_organization"
    return None


def needs_fresh_sources(topic: str, instructions: str = "") -> bool:
    return bool(_FRESH.search(f"{topic} {instructions}"))


def is_general_or_creative(topic: str, instructions: str = "") -> bool:
    """Only recognize explicit creative or general explanatory drafting."""
    text = f"{topic} {instructions}"
    return bool(_CREATIVE.search(text) or _GENERAL.search(topic)
                or re.search(r"\b(nasıl|nedir|neden)\b", topic, re.IGNORECASE))
