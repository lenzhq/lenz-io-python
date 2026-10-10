"""Keep ``AsyncLenz``'s docstrings in step with ``Lenz``'s.

Every docstring of the async client (``src/lenz_io/async_client.py``) is the
sync client's (``src/lenz_io/client.py``) for the same class and method, with
the calls written for asyncio (``await client.x(...)``, ``async for``,
``AsyncLenz``, ``httpx.AsyncClient``, ``aclose``) and, on the waits, a
paragraph on what only the async client does (awaitable callbacks,
``cancel_on_abort``).

    python scripts/sync_async_docstrings.py --check   # CI: exit 1 on any drift
    python scripts/sync_async_docstrings.py --write   # rewrite the async docstrings

Edit a docstring in ``client.py`` (or a rule here), then run ``--write``.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYNC = ROOT / "src" / "lenz_io" / "client.py"
ASYNC = ROOT / "src" / "lenz_io" / "async_client.py"

#: Sync class -> async class.
CLASSES = {
    "Lenz": "AsyncLenz",
    "_VerificationsNamespace": "_AsyncVerificationsNamespace",
    "_AskNamespace": "_AsyncAskNamespace",
    "_LibraryNamespace": "_AsyncLibraryNamespace",
}
#: Sync method -> async method, where the name differs.
METHODS = {"close": "aclose"}
#: Async docstrings written for the async client alone (not copies).
OWN = {("AsyncLenz", "with_options"), ("AsyncLenz", "aclose")}

#: Literal rewrites, in order, before the general ones.
LITERAL = [
    # The ladder example: ``.claims`` belongs to the awaited result.
    (
        "for row in client.assess(claims=claims[i : i + 20]).claims]",
        "for row in (await client.assess(claims=claims[i : i + 20])).claims]",
    ),
    ("fast.usage()", "await fast.usage()"),
    ("traced.assess(", "await traced.assess("),
    ("``close()`` and ``with`` on a copy do", "``aclose()`` and ``async with`` on a copy do"),
    (
        "copy is as safe to share across threads as the client.",
        "copy belongs to the client's event loop, as the client does.",
    ),
    ("Block until an already-submitted task terminates", "Wait until an already-submitted task terminates"),
    ("for item in client.", "async for item in client."),
]

#: ``client.a.b(`` -> ``await client.a.b(``, except an ``iter(`` (an async
#: iterator is not awaited), ``with_options(`` (a plain method) and a call
#: already awaited.
_CALL = re.compile(r"(?<![\w.])(?<!await )client\.((?:\w+\.)*\w+)\(")
_NOT_AWAITED = ("iter", "with_options")


def _await_call(match: re.Match[str]) -> str:
    chain = match.group(1)
    if chain.rsplit(".", 1)[-1] in _NOT_AWAITED:
        return match.group(0)
    return f"await client.{chain}("


_CALLBACK = (
    "``{name}`` may also be a coroutine function, or return an awaitable: it is awaited before the poll continues."
)
_ABORT = (
    "``cancel_on_abort`` (default ``False``, async only): when the task awaiting this call is cancelled "
    "after the {job} exists, also stop it on the server: one ``{cancel}`` request{per}, no retry, all within "
    "5 seconds, and then the ``CancelledError`` is re-raised unchanged. {charge} A cancel that fails, or "
    "that loses the race to the end of the {job} (it then answers with the finished result, charged as "
    "usual), is logged at WARNING with the {job_id}. A cancellation from outside counts (a client that "
    "disconnected, ``asyncio.timeout`` or ``asyncio.wait_for``, which may then overrun by up to those 5 "
    "seconds); this call's own ``timeout`` running out does not, and never stops the {job}. Without the "
    "flag a cancelled await only stops waiting: the {job} keeps running and is charged as usual."
)
_VERIFY_CHARGE = "A cancelled verification is not charged."
_JOB_CHARGE = "A cancelled {job} refunds what it had not delivered; what it delivered is charged."

#: What only the async client does, appended to a method's docstring.
ADDENDA = {
    ("AsyncLenz", "verify_and_wait"): [
        _CALLBACK.format(name="on_progress"),
        _ABORT.format(job="verification", cancel="cancel(task_id)", per="", charge=_VERIFY_CHARGE, job_id="task id"),
    ],
    ("AsyncLenz", "wait"): [
        _CALLBACK.format(name="on_progress"),
        _ABORT.format(job="verification", cancel="cancel(task_id)", per="", charge=_VERIFY_CHARGE, job_id="task id"),
        "The id is known up front: with ``cancel_on_abort``, a cancellation already requested when the wait "
        "starts cancels the run without polling it. A task cancelled before it ever runs never enters the "
        "call, so nothing is sent.",
    ],
    ("AsyncLenz", "verify_batch_and_wait"): [
        _CALLBACK.format(name="on_progress"),
        _ABORT.format(
            job="verification",
            cancel="cancel(task_id)",
            per=" per item not yet finished, sent together",
            charge="A cancelled verification is not charged.",
            job_id="task id",
        ).replace("after the verification exists", "after the batch was accepted"),
    ],
    ("AsyncLenz", "review_and_wait"): [
        _CALLBACK.format(name="on_update"),
        _ABORT.format(
            job="review",
            cancel="cancel_review(review_id)",
            per="",
            charge=_JOB_CHARGE.format(job="review"),
            job_id="review id",
        ),
    ],
    ("AsyncLenz", "citecheck_and_wait"): [
        _CALLBACK.format(name="on_update"),
        _ABORT.format(
            job="citation check",
            cancel="cancel_citecheck(citecheck_id)",
            per="",
            charge=_JOB_CHARGE.format(job="citation check"),
            job_id="citecheck id",
        ),
    ],
    ("AsyncLenz", None): [
        "The asyncio client: every method of :class:`lenz_io.Lenz`, with the same parameters, defaults and "
        "results, as a coroutine. Use it as ``async with AsyncLenz() as client:`` or close it with "
        "``await client.aclose()``. One ``AsyncLenz`` belongs to one event loop, like the "
        "``httpx.AsyncClient`` inside it: create it where it is used (in an app's lifespan, not at import time).",
    ],
}


def _wrap(text: str, indent: str, width: int = 100) -> list[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        if line and len(indent) + len(line) + 1 + len(word) > width:
            lines.append(indent + line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        lines.append(indent + line)
    return lines


def _docstrings(tree: ast.Module, source: str) -> dict[tuple[str, str | None], ast.Expr]:
    """``(class, method or None)`` -> the docstring expression node."""
    out: dict[tuple[str, str | None], ast.Expr] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if _is_doc(node.body[0]):
            out[(node.name, None)] = node.body[0]  # type: ignore[assignment]
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.body and _is_doc(item.body[0]):
                if any(isinstance(d, ast.Name) and d.id == "overload" for d in item.decorator_list):
                    continue
                out[(node.name, item.name)] = item.body[0]  # type: ignore[assignment]
    return out


def _is_doc(node: ast.stmt) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _segment(lines: list[str], node: ast.Expr) -> str:
    assert node.end_lineno is not None
    first = lines[node.lineno - 1][node.col_offset :]
    return "\n".join([first, *lines[node.lineno : node.end_lineno]])


def render(sync_text: str, key: tuple[str, str | None], indent: str) -> str:
    text = sync_text
    for old, new in LITERAL:
        text = text.replace(old, new)
    text = _CALL.sub(_await_call, text)
    text = text.replace(":meth:`Lenz.", ":meth:`AsyncLenz.")
    text = re.sub(r"(?<![\w.`])Lenz\(", "AsyncLenz(", text)
    text = text.replace("httpx.Client", "httpx.AsyncClient")
    for paragraph in ADDENDA.get(key, []):
        body = "\n".join(_wrap(paragraph, indent))
        if text.endswith(f'\n{indent}"""'):
            text = text[: -len(f'{indent}"""')] + f'\n{body}\n{indent}"""'
        else:
            text = text[:-3] + f'\n\n{body}\n{indent}"""'
    return text


def expected() -> tuple[str, list[str]]:
    """The async module's source with every docstring as it should be, and
    the keys that have no sync counterpart (an error)."""
    sync_src = SYNC.read_text()
    async_src = ASYNC.read_text()
    sync_lines, async_lines = sync_src.split("\n"), async_src.split("\n")
    sync_docs = _docstrings(ast.parse(sync_src), sync_src)
    async_docs = _docstrings(ast.parse(async_src), async_src)
    by_async: dict[tuple[str, str | None], tuple[str, str | None]] = {}
    for cls, method in sync_docs:
        by_async[(CLASSES.get(cls, cls), METHODS.get(method, method) if method else None)] = (cls, method)
    missing: list[str] = []
    edits: list[tuple[int, int, str]] = []
    for key, node in async_docs.items():
        source = by_async.get(key)
        if key[0] not in CLASSES.values() or key in OWN:
            continue
        if source is None:
            # Async-only private helpers keep their own docstrings.
            if key[1] is not None and key[1].startswith("_"):
                continue
            missing.append(f"{key[0]}.{key[1]}")
            continue
        indent = " " * node.col_offset
        text = render(_segment(sync_lines, sync_docs[source]), key, indent)
        assert node.end_lineno is not None
        edits.append((node.lineno, node.end_lineno, indent + text))
    for start, end, text in sorted(edits, reverse=True):
        async_lines[start - 1 : end] = text.split("\n")
    return "\n".join(async_lines), missing


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "--check"
    want, missing = expected()
    if missing:
        sys.stderr.write(f"async docstrings with no sync counterpart: {', '.join(missing)}\n")
        return 1
    if mode == "--write":
        ASYNC.write_text(want)
        return 0
    if ASYNC.read_text() != want:
        sys.stderr.write(
            "AsyncLenz's docstrings differ from Lenz's: run python scripts/sync_async_docstrings.py --write\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
