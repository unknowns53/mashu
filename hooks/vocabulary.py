"""Coinages this project's documents used to carry, for ~/.claude/hooks/guard-vocabulary.py.

The user-level guard loads this file for writes and replies in sessions of this project, and
the stop-ai-slop-jp skill's scan_coinages.py reads it when it lists candidates. Each word was
a short Japanese stand-in for an English term or an identifier, such as 台帳 for the ledger
table or 退役 for the retire operation. Later sessions copied them from the documents, so
the guard stops them coming back.

ALLOW lists words that a scan reported and that turned out to be ordinary Japanese here.
A word inside backquotes or 「」 is a mention rather than a use and passes.
"""
import re

RULES = [
    {"pattern": r"(?<![一-鿿])欄(?![一-鿿])", "meaning": "field", "instead": "フィールド、引数"},
    {"pattern": r"(?<![一-鿿])枠(?![一-鿿])", "meaning": "budget slot", "instead": "上限、予算、`card_limit`"},
    {"pattern": r"(?<![一-鿿])席(?![一-鿿])|席数|定員", "meaning": "seat (budget treated as fixed capacity)",
     "instead": "容量"},
    {"pattern": r"在庫", "meaning": "stock (all active memories)", "instead": "Active な Memory の総量"},
    {"pattern": r"台帳", "meaning": "ledger table", "instead": "`ledger`"},
    {"pattern": r"痕跡", "meaning": "trace", "instead": "trace"},
    {"pattern": r"退役", "meaning": "retire operation", "instead": "retire、retire された Memory"},
    {"pattern": r"身分", "meaning": "kind of record", "instead": "種類、扱い"},
    {"pattern": r"入場", "meaning": "admit", "instead": "登録、登録し直し"},
    {"pattern": r"凍結", "meaning": "freeze a snapshot", "instead": "スナップショットとして保存する"},
    {"pattern": r"(?<![一-鿿])腐[らりるれっ]", "meaning": "rot", "instead": "古くなる、事実と合わなくなる"},
    {"pattern": r"掃き出", "meaning": "sweep", "instead": "期限切れの項目の一覧"},
    {"pattern": r"(?<![一-鿿])(?:嘘|蔵|門)(?![一-鿿])", "meaning": "lie, store, gate",
     "instead": "誤った情報、Mashu の DB、hook の検査"},
]

ALLOW = {"配信", "常設", "写し", "系", "穴", "筋", "霧", "印"}

_MENTION = re.compile(r"`[^`\n]*`|「[^」\n]*」")
_COMPILED = [(re.compile(rule["pattern"]), rule) for rule in RULES]


def find(text: str, path: str = "") -> list:
    """Returns (line_number, word, excerpt, rule) for each coinage in text."""
    hits = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = _MENTION.sub(lambda m: " " * len(m.group(0)), raw)
        for regex, rule in _COMPILED:
            for match in regex.finditer(line):
                start, end = match.span()
                hits.append((number, raw[start:end], raw[max(0, start - 12):end + 8].strip(), rule))
    return hits


def report(hits: list, label: str) -> str:
    return "\n".join(f"{label}:{number}: 「{word}」 ({rule['meaning']}) … {excerpt} → {rule['instead']}"
                     for number, word, excerpt, rule in hits)
