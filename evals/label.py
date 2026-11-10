"""
Work through the review queue with single keypresses: ``j`` junk, ``s`` scam, ``l`` legit, ``k`` skip, ``q`` quit.

Each item shows why it was queued, the facts that matter and a short excerpt of the page text. A decision is saved
immediately as ``label_source="human"`` and the item leaves the queue, so quitting halfway loses nothing.

Usage: ``uv run python -m evals.label`` (or ``make label``).
"""

import sys
import termios
import textwrap
import tty

from evals.corpus import Corpus, Label, LabelRecord, QueueItem
from unfret.html_cleaner import clean_html

KEYS: dict[str, Label] = {"j": "junk", "s": "scam", "l": "legit"}
_EXCERPT_CHARS = 900


def read_key() -> str:
    """Read one keypress from the terminal without waiting for Enter."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def describe(item: QueueItem, record: LabelRecord, page_text: str) -> str:
    """
    Render one queue item as a compact screen of text.

    :param item: The queue item.
    :param record: Its label record.
    :param page_text: The page's visible text.
    :return: The text to print.
    """
    excerpt = textwrap.shorten(" ".join(page_text.split()), _EXCERPT_CHARS, placeholder=" …")
    lines = [
        f"{record.id}  ·  {record.platform or 'own shop'}  ·  {record.origin_dataset}",
        f"origin: {record.origin_url}",
        f"why queued: {item.reason}",
        f"prior: {record.prior_label}  ·  LLM: {record.llm_label} ({record.llm_confidence:.2f})",
    ]
    if record.notes.startswith("{"):
        lines.append(f"rule features: {record.notes[:300]}")
    lines += ["", textwrap.fill(excerpt, 110), "", "[j]unk  [s]cam  [l]egit  [k] skip  [q]uit"]
    return "\n".join(lines)


def apply_decision(record: LabelRecord, label: Label) -> LabelRecord:
    """
    Turn a keypress decision into the record's final human label.

    :param record: The record being decided.
    :param label: The chosen label.
    :return: The updated record.
    """
    return record.model_copy(update={"label": label, "label_source": "human", "confidence": 1.0})


def main() -> None:
    """Command-line entry point."""
    corpus = Corpus()
    queue = corpus.queue()
    if not queue:
        print("The review queue is empty.")  # noqa: T201 - interactive tool
        return
    remaining = list(queue)
    for n, item in enumerate(queue, start=1):
        record = corpus.labels().get(item.id)
        if record is None:
            remaining.remove(item)
            continue
        print(f"\n── {n}/{len(queue)} " + "─" * 60)  # noqa: T201 - interactive tool
        print(describe(item, record, clean_html(corpus.read_page(record.id))))  # noqa: T201 - interactive tool
        key = read_key().lower()
        if key == "q":
            break
        if key in KEYS:
            corpus.update([apply_decision(record, KEYS[key])])
            remaining.remove(item)
            corpus.write_queue(remaining)
            print(f"→ {KEYS[key]}")  # noqa: T201 - interactive tool
    print(f"\n{len(remaining)} item(s) left in the queue.")  # noqa: T201 - interactive tool


if __name__ == "__main__":
    main()
