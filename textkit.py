# textkit.py
"""
Чистые текстовые утилиты без обращений к сети и ИИ (поэтому не тратят лимит):
  • наглядный diff правок (зачёркнуто — удалено, жирным — добавлено);
  • разбор и подстановка имён собеседников;
  • нарезка текста на части для длинных расшифровок.
"""

import re
import html
import difflib
from typing import Dict, List, Tuple

_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_TAG_RE = re.compile(r"</?(?:b|i|u|s|code|pre|a|strong|em)(?:\s[^>]*)?>", re.IGNORECASE)


def strip_markup(text: str) -> str:
    """HTML от sanitize_llm_output → обычный текст (для сравнения и экспорта)."""
    return html.unescape(_TAG_RE.sub("", text or ""))


# ============================================================================
# DIFF
# ============================================================================

def _tokenize(text: str) -> List[Tuple[str, str]]:
    """[(токен, пробелы перед ним)] — слова и знаки препинания отдельно."""
    out, pos = [], 0
    for m in _TOKEN_RE.finditer(text):
        out.append((m.group(0), text[pos:m.start()]))
        pos = m.end()
    return out


def _join(tokens: List[Tuple[str, str]], one_line: bool = False) -> str:
    s = "".join(ws + tok for tok, ws in tokens)
    return re.sub(r"\s+", " ", s) if one_line else s


def _wrap(tag: str, text: str, piece: int = 1500) -> List[str]:
    """<tag>text</tag>; слишком длинный кусок режется по пробелам, каждый кусок закрыт."""
    text = text.strip()
    if not text:
        return []
    parts, cur = [], ""
    for w in re.split(r"(?<=\s)", text):
        if len(cur) + len(w) > piece and cur:
            parts.append(cur)
            cur = ""
        cur += w
    if cur:
        parts.append(cur)
    return [f"<{tag}>{html.escape(p.strip(), quote=False)}</{tag}>" for p in parts if p.strip()]


def diff_stats_and_chunks(original: str, corrected: str) -> Tuple[int, int, int, List[Tuple[bool, str]]]:
    """
    Возвращает (правок, удалено слов, добавлено слов, части [(изменение?, html)]).
    Части — законченные куски HTML: ими можно безопасно делить сообщение на страницы.
    """
    a, b = _tokenize(original), _tokenize(corrected)
    sm = difflib.SequenceMatcher(None, [t for t, _ in a], [t for t, _ in b], autojunk=False)
    chunks: List[Tuple[bool, str]] = []
    changes = del_words = ins_words = 0

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            chunks.append((False, html.escape(_join(b[j1:j2]), quote=False)))
            continue
        changes += 1
        old = _join(a[i1:i2], one_line=True)
        new = _join(b[j1:j2])
        lead = b[j1][1] if j1 < len(b) else ""
        # пробелы/переводы строк перед правкой берём из исправленного текста
        prefix = html.escape(lead, quote=False) if lead else ""
        pieces: List[str] = []
        if old.strip():
            pieces += _wrap("s", old)
            del_words += sum(1 for t, _ in a[i1:i2] if t[0].isalnum() or t[0] == "_")
        if new.strip():
            pieces += _wrap("b", new)
            ins_words += sum(1 for t, _ in b[j1:j2] if t[0].isalnum() or t[0] == "_")
        chunks.append((True, prefix + " ".join(pieces)))
    return changes, del_words, ins_words, chunks


def _compact(chunks: List[Tuple[bool, str]], context_chars: int = 90) -> List[str]:
    """Оставляет изменения и немного контекста вокруг, длинные совпадения сворачивает в «…»."""
    out: List[str] = []
    n = len(chunks)
    for idx, (changed, text) in enumerate(chunks):
        if changed:
            out.append(text)
            continue
        if len(text) <= context_chars * 2 + 20:
            out.append(text)
            continue
        head = text[:context_chars] if idx > 0 else ""
        tail = text[-context_chars:] if idx < n - 1 else ""
        # не режем HTML-сущности (&amp; и т.п.)
        head = re.sub(r"&[a-z#0-9]*$", "", head)
        tail = re.sub(r"^[a-z#0-9]*;", "", tail)
        out.append((head + " … " + tail) if head and tail else (head + " …" if head else "… " + tail))
    return out


def make_diff_pages(original: str, corrected: str, limit: int = 3800, max_pages: int = 3) -> Tuple[List[str], int]:
    """
    Страницы с наглядным diff для Telegram (HTML) и число правок.
    Короткий текст показывается целиком; длинный — только изменённые места с контекстом.
    """
    changes, dw, iw, chunks = diff_stats_and_chunks(original, corrected)
    legend = "<s>зачёркнуто</s> — удалено · <b>жирным</b> — добавлено"
    if changes == 0:
        return ["✅ Правок нет: текст совпадает с оригиналом."], 0

    head = f"🔍 <b>Что изменилось</b>\nПравок: <b>{changes}</b> · слов удалено: {dw}, добавлено: {iw}\n{legend}\n\n"
    full = "".join(t for _, t in chunks)
    if len(head) + len(full) <= limit:
        return [head + full], changes

    note = "<i>Текст длинный: показаны только изменённые места.</i>\n\n"
    parts = _compact(chunks)
    pages: List[str] = []
    cur = head + note
    for p in parts:
        if len(cur) + len(p) > limit and cur.strip():
            pages.append(cur)
            cur = ""
        cur += p
    if cur.strip():
        pages.append(cur)
    if len(pages) > max_pages:
        pages = pages[:max_pages]
        pages[-1] += "\n\n<i>…остальные правки не поместились. Скачайте результат файлом и сравните в редакторе.</i>"
    return pages, changes


# ============================================================================
# ИМЕНА СОБЕСЕДНИКОВ
# ============================================================================

_SPEAKER_RE = re.compile(r"Говорящий\s*(\d+)")


def speaker_numbers(text: str) -> List[int]:
    return sorted({int(n) for n in _SPEAKER_RE.findall(text or "")})


def parse_speaker_names(user_text: str, max_names: int = 10) -> Dict[int, str]:
    """
    «Анна, Игорь» → {1: Анна, 2: Игорь};  «1=Анна, 3=Пётр» → {1: Анна, 3: Пётр}.
    Разделители: запятая, точка с запятой, перевод строки.
    """
    names: Dict[int, str] = {}
    for pos, item in enumerate([i for i in re.split(r"[\n,;]+", user_text or "") if i.strip()], start=1):
        item = item.strip()
        m = re.match(r"^(?:говорящий\s*)?(\d{1,2})\s*[=:)\-–—]\s*(.+)$", item, re.IGNORECASE)
        num, name = (int(m.group(1)), m.group(2)) if m else (pos, item)
        name = re.sub(r"[<>]", "", name)                 # угловые скобки в именах не нужны
        name = re.sub(r"\s+", " ", name).strip(" \"'«»")[:40]
        if name and 1 <= num <= 99 and len(names) < max_names:
            names[num] = name
    return names


def apply_speaker_names(text: str, names: Dict[int, str]) -> str:
    """Подставляет имена вместо «Говорящий N» в уже санитизированном HTML (имена экранируются)."""
    def repl(m):
        n = int(m.group(1))
        return html.escape(names[n], quote=False) if n in names else m.group(0)
    return _SPEAKER_RE.sub(repl, text or "")


# ============================================================================
# НАРЕЗКА ДЛИННЫХ РАСШИФРОВОК
# ============================================================================

def split_sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.!?…])\s+|\n+", (text or "").strip())
    return [p.strip() for p in parts if p and p.strip()]


def chunk_lines(lines: List[str], max_chars: int) -> List[str]:
    """Склеивает строки в части не длиннее max_chars (строка длиннее лимита режется по пробелам)."""
    chunks: List[str] = []
    cur: List[str] = []
    size = 0
    for line in lines:
        pieces = [line]
        if len(line) > max_chars:
            pieces = [line[i:i + max_chars] for i in range(0, len(line), max_chars)]
        for piece in pieces:
            if size + len(piece) + 1 > max_chars and cur:
                chunks.append("\n".join(cur))
                cur, size = [], 0
            cur.append(piece)
            size += len(piece) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


# ============================================================================
# НАРЕЗКА ДЛИННОГО HTML ДЛЯ TELEGRAM (лимит сообщения — 4096 знаков)
# ============================================================================

_HTML_TAG_RE = re.compile(r"<(/?)(b|i|u|s|code|pre|a)(\s[^>]*)?>", re.IGNORECASE)


def _open_tags_after(piece: str, stack: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Какие теги остались открытыми после piece. stack — [(имя, исходный открывающий тег)]."""
    stack = list(stack)
    for m in _HTML_TAG_RE.finditer(piece):
        name = m.group(2).lower()
        if m.group(1):
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == name:
                    del stack[i:]
                    break
        else:
            stack.append((name, m.group(0)))
    return stack


def _safe_cut(text: str, limit: int) -> int:
    """Позиция разреза ≤ limit: по абзацу, строке, предложению, пробелу; не внутри тега и &сущности;."""
    if len(text) <= limit:
        return len(text)
    window = text[:limit]
    cut = -1
    for sep, min_pos in (("\n\n", 0.3), ("\n", 0.4), (". ", 0.5), ("! ", 0.5), ("? ", 0.5), ("; ", 0.5), (", ", 0.6), (" ", 0.6)):
        pos = window.rfind(sep)
        if pos >= limit * min_pos:
            cut = pos + len(sep.rstrip(" ")) if sep.strip() else pos
            break
    if cut <= 0:
        cut = limit
    # не режем внутри тега «<b …» и внутри «&amp;»
    lt, gt = window[:cut].rfind("<"), window[:cut].rfind(">")
    if lt > gt:
        cut = lt
    amp = window[:cut].rfind("&")
    if amp != -1 and ";" not in window[amp:cut] and cut - amp < 10:
        cut = amp
    return max(cut, 1)


def split_html(text: str, limit: int = 3800, first_limit: int = 0) -> List[str]:
    """
    Делит длинный HTML-текст на части ≤ limit знаков (первая — ≤ first_limit, если задан).
    Режет по абзацам/предложениям; теги, открытые на границе, закрываются в конце
    части и открываются заново в начале следующей, поэтому каждая часть валидна для Telegram.
    """
    text = (text or "").strip()
    if not text:
        return []
    parts: List[str] = []
    stack: List[Tuple[str, str]] = []
    first = True
    while text:
        lim = first_limit if (first and first_limit) else limit
        prefix = "".join(t for _, t in stack)
        reserve = 20 * len(stack) + 12
        room = max(200, lim - len(prefix) - reserve)
        if len(prefix) + len(text) + len("".join(f"</{n}>" for n, _ in stack)) <= lim:
            parts.append(prefix + text)
            break
        cut = _safe_cut(text, room)
        piece, text = text[:cut], text[cut:].lstrip("\n ")
        new_stack = _open_tags_after(prefix + piece, [])
        closing = "".join(f"</{n}>" for n, _ in reversed(new_stack))
        parts.append(prefix + piece.rstrip() + closing)
        stack = new_stack
        first = False
    return [p for p in parts if p.strip()]
