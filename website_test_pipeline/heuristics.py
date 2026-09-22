"""The word lists and selectors the tool uses to read plain sentences and unfamiliar pages.

Nothing here belongs to one website or one language. The defaults are deliberately generic (standard
ARIA roles, the loading-spinner names most sites use, common English verbs), and every list can be
extended or replaced for a site without touching code, in runs/<site>/heuristics.json:

    {
      "content_words":   ["résultats", "liste"],        # ADDED to the defaults
      "pick_verbs":      ["choisit", "sélectionne"],
      "menu_selectors":  ["[data-menu-open]"],
      "replace": ["trigger_stopwords"],                # these keys REPLACE the defaults instead of extending them
      "unsuitable": [{"pattern": "connexion", "reason": "needs credentials"}]
    }

A word may end in * to match any ending ("result*" -> result, results, resulting). The file is optional:
a missing, unreadable or malformed file just means the defaults are used (and a note says so).

The active file comes from configure(path) (the CLI calls it) or, for the generated specs that pytest
runs in another process, from the WTP_HEURISTICS environment variable, so the runner that verified a
flow and the spec that tests it always read the same lists.
"""
from __future__ import annotations
import json
import os
import re
from pathlib import Path

DEFAULTS: dict[str, list] = {
    # a sentence that says the visitor sees/finds/gets any of these promises content, not just a new URL
    "content_words": ["result*", "list", "listing", "table", "detail*", "see", "sees", "show*", "display*", "find*", "found"],
    # verbs that say "choose an option" - used to decide that a click on a menu button should be a pick
    "pick_verbs": ["pick*", "choose*", "chosen", "select*", "filter*"],
    # words in a menu button's name that say nothing about what the menu holds
    "trigger_stopwords": ["please", "select", "choose", "pick", "your", "the"],
    # an option that means "everything" (never a sensible default choice), and the verbs that can precede it
    "all_option_words": ["all", "none", "everything"],
    "all_option_prefixes": ["select", "check", "uncheck", "deselect", "clear"],
    # query-string parameters that change on every visit; never asserted. A leading = means the exact name.
    "volatile_params": ["utm_", "_ga", "fbclid", "gclid", "=sid", "session", "token", "nonce", "=ts", "time", "=cb", "rand", "=_"],
    # CSS class fragments that mark a loading spinner
    "loader_hints": ["loading", "loader", "spinner"],
    # open dropdown / menu containers (visible ones), and what an option inside one looks like
    "menu_selectors": [".ui-multiselect-menu", ".select2-dropdown", ".select2-results", "[class*=\"dropdown-menu\"]",
                       "[role=\"listbox\"]", "[role=\"menu\"]"],
    "option_selectors": ["li label", "li [role=\"option\"]", "li a", "[role=\"option\"]", "[role=\"menuitemcheckbox\"]",
                         "[role=\"menuitemradio\"]", "[role=\"menuitem\"]"],
    # Unicode codepoint ranges (hex, inclusive, "START-END") of right-to-left scripts, used to detect a
    # page's actual writing direction from its captured text - not from a language code or URL pattern,
    # which a site could get wrong. Covers Hebrew, Arabic (+ supplement/extended-A/presentation forms),
    # Syriac, Thaana and N'Ko; add more (e.g. Samaritan, Mandaic) for a site that needs them.
    "rtl_script_ranges": ["0590-05FF", "FB1D-FB4F", "0600-06FF", "0750-077F", "08A0-08FF", "FB50-FDFF",
                          "FE70-FEFF", "0700-074F", "0780-07BF", "07C0-07FF"],
    # sentences the AI may not write, as {"pattern": regex, "reason": text}
    "unsuitable": [
        {"pattern": "\\b(password|passcode|log ?in|sign ?in|sign ?up|credit card|payment|checkout)\\b",
         "reason": "needs credentials or payment details"},
        {"pattern": "\\b(controls?|elements?|widgets?|fields?|options?)\\b[^.]{0,20}\\b(appear|show|display|become visible)",
         "reason": "describes widgets appearing, not something a visitor achieves"},
        {"pattern": "\\b(language|arabic version|english version|switch(es)? to (arabic|english))\\b",
         "reason": "a language switch leaves the site and cannot be tested"},
    ],
}

_config: dict[str, list] | None = None
_notes: list[str] = []
_cache: dict = {}


def _load(path: Path | None) -> tuple[dict[str, list], list[str]]:
    merged = {k: list(v) for k, v in DEFAULTS.items()}
    notes: list[str] = []
    if not path or not Path(path).is_file():
        return merged, notes
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("the file must hold a JSON object")
    except (OSError, ValueError) as exc:
        return merged, [f"{path} ignored ({exc}); using the default heuristics"]
    replace = {k for k in data.get("replace", []) if isinstance(k, str)} if isinstance(data.get("replace", []), list) else set()
    for key, value in data.items():
        if key == "replace":
            continue
        if key not in DEFAULTS:
            notes.append(f"unknown heuristics key '{key}' ignored")
            continue
        if not isinstance(value, list):
            notes.append(f"heuristics key '{key}' must be a list; ignored")
            continue
        good = [v for v in value if (isinstance(v, str) and v.strip()) or (key == "unsuitable" and isinstance(v, dict) and v.get("pattern"))]
        if key == "unsuitable":
            good = [v for v in good if isinstance(v, dict) and _valid_regex(v["pattern"], notes)]
        merged[key] = good if key in replace else merged[key] + good
    return merged, notes


def _valid_regex(pattern: str, notes: list[str]) -> bool:
    try:
        re.compile(pattern)
        return True
    except re.error as exc:
        notes.append(f"unsuitable pattern {pattern!r} ignored ({exc})")
        return False


def configure(path: Path | str | None) -> list[str]:
    """Use the heuristics file at `path` (None = defaults only). Returns notes about anything ignored."""
    global _config, _notes
    _config, _notes = _load(Path(path) if path else None)
    _cache.clear()
    return list(_notes)


def _active() -> dict[str, list]:
    if _config is None:
        env = os.environ.get("WTP_HEURISTICS")
        configure(env if env else None)
    return _config or {}


def notes() -> list[str]:
    _active()
    return list(_notes)


def get(key: str) -> list:
    return list(_active().get(key, []))


def words(key: str) -> list[str]:
    return [w for w in get(key) if isinstance(w, str)]


def _word_pattern(word: str) -> str:
    """One word as a regex: literal, except a trailing * which means 'any ending'."""
    core = word.strip().lower()
    return re.escape(core[:-1]) + "[^\\W_]*" if core.endswith("*") else re.escape(core)


def word_regex(key: str) -> re.Pattern:
    """Case-insensitive regex matching any word of the list as a whole word (cached until reconfigured)."""
    listed = tuple(words(key))
    cached = _cache.get(("word", key, listed))
    if cached is None:
        body = "|".join(_word_pattern(w) for w in listed) or "(?!)"
        cached = _cache[("word", key, listed)] = re.compile("(?<![^\\W_])(?:" + body + ")(?![^\\W_])", re.I)
    return cached


def has_word(key: str, text: str) -> bool:
    return bool(word_regex(key).search(text or ""))


def all_option_regex() -> re.Pattern:
    """Matches an option that means 'everything', e.g. 'Select All' or 'none'."""
    prefixes, tail = tuple(words("all_option_prefixes")), tuple(words("all_option_words"))
    cached = _cache.get(("all", prefixes, tail))
    if cached is None:
        pre = "|".join(_word_pattern(w) for w in prefixes) or "(?!)"
        end = "|".join(_word_pattern(w) for w in tail) or "(?!)"
        cached = _cache[("all", prefixes, tail)] = re.compile("^(?:(?:" + pre + ")\\s*)?(?:" + end + ")$", re.I)
    return cached


def is_volatile_param(name: str) -> bool:
    key = (name or "").lower()
    for item in words("volatile_params"):
        item = item.lower()
        if (item.startswith("=") and key == item[1:]) or (not item.startswith("=") and key.startswith(item)):
            return True
    return False


def unsuitable_rules() -> list[tuple[re.Pattern, str]]:
    rules = []
    for item in get("unsuitable"):
        if isinstance(item, dict) and item.get("pattern"):
            rules.append((re.compile(item["pattern"], re.I), str(item.get("reason") or "not suitable")))
    return rules


def menu_selector() -> str:
    """CSS selecting any open menu container that is visible."""
    return ", ".join(f"{s}:visible" for s in words("menu_selectors"))


def option_selector() -> str:
    return ", ".join(words("option_selectors"))


def _rtl_char_class() -> str:
    parts = []
    for item in words("rtl_script_ranges"):
        match = re.match(r"^([0-9A-Fa-f]{4,6})-([0-9A-Fa-f]{4,6})$", item.strip())
        if match:
            parts.append(f"\\U{int(match.group(1), 16):08x}-\\U{int(match.group(2), 16):08x}")
    return "".join(parts)


def rtl_script_regex() -> re.Pattern:
    """Matches one character of a right-to-left script (see rtl_script_ranges), cached until reconfigured."""
    listed = tuple(words("rtl_script_ranges"))
    cached = _cache.get(("rtl", listed))
    if cached is None:
        cls = _rtl_char_class()
        cached = _cache[("rtl", listed)] = re.compile("[" + cls + "]") if cls else re.compile("(?!)")
    return cached


def dominant_script(text: str) -> str:
    """'rtl' when right-to-left-script characters are the majority of the alphabetic characters in `text`,
    'ltr' when they are a minority, 'unknown' when there is no alphabetic content to judge from at all
    (numbers, icons, an empty string) - never guessed on text too thin to say anything about."""
    rtl = len(rtl_script_regex().findall(text or ""))
    alpha = sum(1 for ch in (text or "") if ch.isalpha())
    ltr = alpha - rtl
    if rtl == 0 and ltr == 0:
        return "unknown"
    return "rtl" if rtl >= ltr else "ltr"


def loader_selector() -> str:
    hints = "".join(f'[class*="{h}" i],' for h in words("loader_hints"))
    return hints + '[aria-busy="true"],[role="progressbar"]'
