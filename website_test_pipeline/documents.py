"""Attached requirement documents as evidence for journeys: `intents <file> [<file> ...]`.

A person often already has the answer to "what should be tested?": a requirements document, a user-story
list, a specification. Here the AI reads such a document and turns what it states into the same plain
sentences `intents` writes, which then go through the same expansion, verification and testing. Nothing about
that changes what a test may claim.

Code stays in charge of what the model may claim:
  * the text is extracted by code (txt, md, rst, docx, pdf, or a .py of constants + a REQUIREMENTS template),
    never by the model;
  * every proposed journey must carry a `quote` copied from the document, and it is accepted only if that
    quote really is in the text (ignoring case, spacing and punctuation) - the model cannot invent a
    requirement and attribute it to the document;
  * the journey must start on a page the explorer visited, and pass the same wording, duplicate and
    unsuitable-topic checks as any AI sentence (intents.accept_intents);
  * a requirement the explored pages cannot show is simply skipped; expansion later checks every page,
    control and option against the DOM, exactly as for any sentence.
"""
from __future__ import annotations
import ast
import re
from datetime import datetime, timezone
from pathlib import Path

SUPPORTED = (".txt", ".md", ".markdown", ".rst", ".docx", ".pdf", ".py")
MAX_BYTES = 8_000_000            # refuse anything bigger: this is prose, not a data dump
CHUNK_CHARS = 6000               # what the model is shown at a time
MAX_CHUNKS = 4                   # per document and run; a long document is covered over several runs
MIN_QUOTE_WORDS = 4              # a one-word "quote" proves nothing

PROMPT_VERSION = "intents-doc-v1"
MAX_NEW = 8

SYSTEM = "Return exactly one JSON object and no prose. Propose only what the site map supports."

RULES = (
    "You are a QA analyst. A REQUIREMENTS excerpt from a document is given, followed by the SITE MAP of the pages that "
    "were actually explored. Write up to %d user journeys, as plain English sentences, that TEST what the requirements "
    "state: each is something a visitor does on this site, then what they should see.\n"
    "Rules:\n"
    "- Every journey must test something the REQUIREMENTS state. Put in \"quote\" one exact sentence or clause copied "
    "from the requirements that the journey is based on. Do not paraphrase the quote and do not invent requirements.\n"
    "- Only journeys the SITE MAP supports: pages and controls listed there. If a requirement cannot be tested on these "
    "pages, skip it.\n"
    "- NO technical words: no selectors, ids, CSS, URLs, or code. Use the names visitors see on screen.\n"
    "- Never a journey that needs an account, password, payment or a real person's data, a language switch, or one that "
    "only shows widgets appearing. Prefer journeys of two or more actions, or that reach another page.\n"
    "- Do not repeat or reword anything in EXISTING.\n"
    "- start_path is the page the visitor starts on (a path from the SITE MAP). evidence is one sentence citing the page "
    "and controls that make you believe the journey exists.\n"
    'Output: {"intents":[{"sentence":"...","start_path":"/...","evidence":"...","quote":"..."}]}'
) % MAX_NEW


class DocumentError(Exception):
    """A document that cannot be used; the message says what to do."""


# ------------------------------------------------------------------ reading

def read_document(path) -> str:
    """The text of a document. Raises DocumentError when it is missing, unsupported, too big or has no text."""
    file = Path(path)
    if not file.is_file():
        raise DocumentError(f"{file} does not exist")
    suffix = file.suffix.lower()
    if suffix not in SUPPORTED:
        raise DocumentError(f"{file.name}: {suffix or 'no extension'} is not supported (use {', '.join(SUPPORTED)})")
    if file.stat().st_size > MAX_BYTES:
        raise DocumentError(f"{file.name} is larger than {MAX_BYTES // 1_000_000} MB; attach the relevant part")
    try:
        if suffix == ".docx":
            text = _read_docx(file)
        elif suffix == ".pdf":
            text = _read_pdf(file)
        elif suffix == ".py":
            text = _read_py(file)
        else:
            text = file.read_text(encoding="utf-8", errors="replace")
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError(f"{file.name} could not be read ({str(exc).splitlines()[0][:100] if str(exc) else exc.__class__.__name__})") from exc
    text = _tidy(text)
    if len(text.split()) < MIN_QUOTE_WORDS:
        raise DocumentError(f"{file.name} has no readable text (a scanned or image-only file?)")
    return text


def _read_docx(file: Path) -> str:
    from docx import Document
    document = Document(str(file))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(parts)


def _read_py(file: Path) -> str:
    """A requirements file written as Python: plain `NAME = "value"` constants plus a REQUIREMENTS template that
    refers to them as {NAME}. Read with `ast`, never executed. A template line that mentions a variable left
    empty is dropped, so an optional requirement disappears until its value is filled in."""
    try:
        tree = ast.parse(file.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        raise DocumentError(f"{file.name} is not valid Python (line {exc.lineno})") from exc
    values: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                continue
            if isinstance(value, (str, int, float)):
                values[node.targets[0].id] = str(value).strip()
    template = values.pop("REQUIREMENTS", None)
    if template is None:
        raise DocumentError(f'{file.name} needs a REQUIREMENTS = """...""" string')
    lines = []
    for line in template.split("\n"):
        names = re.findall(r"\{(\w+)\}", line)
        unknown = [n for n in names if n not in values]
        if unknown:
            raise DocumentError(f"{file.name}: REQUIREMENTS uses {{{unknown[0]}}} but no variable of that name is set")
        if any(not values[n] for n in names):
            continue
        lines.append(re.sub(r"\{(\w+)\}", lambda m: values[m.group(1)], line))
    return "\n".join(lines)


def _read_pdf(file: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocumentError("reading PDFs needs the pypdf package (pip install pypdf)") from exc
    return "\n".join((page.extract_text() or "") for page in PdfReader(str(file)).pages)


def _tidy(text: str) -> str:
    """Normalise line endings and collapse runs of blank lines; paragraphs stay separated by one blank line."""
    lines = [line.rstrip() for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split at paragraph boundaries into pieces of at most `size` characters; a single paragraph longer than
    that is cut at the size. Order is kept, nothing is dropped."""
    pieces, current = [], ""
    for paragraph in text.split("\n\n"):
        while len(paragraph) > size:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(paragraph[:size])
            paragraph = paragraph[size:]
        if current and len(current) + 2 + len(paragraph) > size:
            pieces.append(current)
            current = ""
        current = f"{current}\n\n{paragraph}" if current else paragraph
    if current.strip():
        pieces.append(current)
    return [p for p in pieces if p.strip()]


# ------------------------------------------------------------------ the quote rule

def _words(text: str) -> list[str]:
    """Words of a text ignoring case, spacing and punctuation (unicode aware)."""
    cleaned = "".join(ch if ch.isalnum() else " " for ch in (text or "").casefold())
    return cleaned.split()


def quote_in_text(quote: str, text: str) -> bool:
    """Is `quote` really in `text`? Compared as word sequences, so quotes, dashes, line breaks and capitalisation
    in the copy do not matter, but a paraphrase or an invented sentence never matches."""
    wanted = _words(quote)
    if len(wanted) < MIN_QUOTE_WORDS:
        return False
    have = _words(text)
    n = len(wanted)
    return any(have[i:i + n] == wanted for i in range(len(have) - n + 1))


# ------------------------------------------------------------------ asking the model

def prompt_for(site_map_text: str, existing: list[str], name: str, part: int, parts: int, excerpt: str,
               uncovered: str = "") -> str:
    shown = "\n".join(f"- {s}" for s in existing[:40]) or "(none yet)"
    extra = f"\n\n{uncovered}" if uncovered else ""
    return (f"{RULES}\n\nREQUIREMENTS (from {name}, part {part} of {parts})\n{excerpt}\n\n"
            f"EXISTING\n{shown}\n\n{site_map_text}{extra}\n\n"
            "Answer now with the JSON object only, starting with { - do not restate the requirements or explain first.")


def run_documents(settings, urls: list[str], client, log, paths: list[str]) -> int:
    """`intents <file>...`. Exit codes as for `intents`: 0 done, 1 nothing usable, 2 no explored pages, 4 model unavailable."""
    from .coverage import compute_coverage, render_uncovered
    from .flows import FlowsFileError, load_flows
    from .intents import IntentsFileError, accept_intents, first_json_object, load_intents, save_intents
    from .llm import is_unavailable
    from .sitemap import build_site_map, load_inventories, render_site_map
    wanted = set(urls)
    inventories = [i for i in load_inventories(settings.artifacts_dir) if i.get("url") in wanted]
    if not inventories:
        log.error("intents: no inventories for the URLs in %s - run explore first", settings.urls_file)
        return 2
    try:
        doc = load_intents(settings.intents_file)
        flows = load_flows(settings.flows_file)["flows"]
    except (IntentsFileError, FlowsFileError) as exc:
        log.error("intents: %s", exc)
        return 1
    site_map = build_site_map(inventories)
    site_map_text = render_site_map(site_map)
    pages = {p["path"] for p in site_map["pages"]}
    uncovered = render_uncovered(compute_coverage(inventories, flows))
    added_total = rejected_total = asked = 0
    for path in paths:
        try:
            text = read_document(path)
        except DocumentError as exc:
            log.error("intents: %s", exc)
            continue
        name = Path(path).name
        parts = chunks(text)
        if len(parts) > MAX_CHUNKS:
            log.info("intents: %s has %d parts; reading the first %d (run again after editing the file to reach the rest)",
                     name, len(parts), MAX_CHUNKS)
        for number, excerpt in enumerate(parts[:MAX_CHUNKS], 1):
            existing = [i["sentence"] for i in doc["intents"] if i.get("status") != "dropped"] + [f.get("goal", "") for f in flows]
            try:
                rows = first_json_object(client.generate(prompt_for(site_map_text, existing, name, number, min(len(parts), MAX_CHUNKS),
                                                                   excerpt, uncovered), SYSTEM))
                rows = [r for r in (rows.get("intents") if isinstance(rows, dict) else None) or [] if isinstance(r, dict)]
            except Exception as exc:
                if is_unavailable(exc):
                    save_intents(settings.intents_file, doc)
                    log.error("intents: the model is unavailable (%s) - kept what was added so far; run it again when it is back", exc)
                    return 4
                log.warning("intents: %s part %d - model answer unusable (%s); skipped", name, number, exc)
                continue
            asked += 1
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            added, rejected = accept_intents(rows, doc, pages, now, settings.model, document=(name, excerpt))
            added_total += len(added)
            rejected_total += len(rejected)
            for intent in added:
                log.info("intents: added %s from %s :: %s", intent["id"], name, intent["sentence"])
            for sentence, reason in rejected:
                log.info("intents: rejected '%s' (%s)", sentence, reason)
            save_intents(settings.intents_file, doc)                 # progress is kept even if a later part fails
    log.info("INTENTS SUMMARY documents=%d asked=%d added=%d rejected=%d file=%s",
             len(paths), asked, added_total, rejected_total, settings.intents_file)
    return 0 if asked else 1
