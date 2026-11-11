"""
A length cap on every other piece of text players read (CLAUDE.md, "Writing
text players read").

The /help pages and the release notes have caps of their own
(tests/test_manual.py, tests/test_changelog.py). Everything else a player
reads - notices, refusals, menus, receipts - is written inline in the code,
so this reads the code itself and fails on any single string over
MAX_MESSAGE_WORDS. Measured at commit 849f772, before this cap existed, the
longest was the first-Diamond note at 92 words.

What it skips:
- data/manual.py and data/changelog.py, which have their own caps;
- docstrings, which players never see;
- SQL, which this codebase writes in capitals, and arguments to logging calls;
- cogs/devtools.py, beta-only internal tooling (see tests/test_manual.py);
- GLOBAL_NOTICES in data/notifications.py, announcements the owner writes in
  their own voice.

A message built from several strings is checked one string at a time, so this
catches one long paragraph, not a card made of several short ones.
"""
import ast
import pathlib
import unittest

from utils.embeds import word_count

REPO = pathlib.Path(__file__).resolve().parent.parent
MAX_MESSAGE_WORDS = 40

SCANNED_DIRECTORIES = ("cogs", "utils", "data")
SKIPPED_FILES = {"data/manual.py", "data/changelog.py", "cogs/devtools.py"}
OWNER_VOICE = {("data/notifications.py", "GLOBAL_NOTICES")}
SQL_KEYWORDS = ("SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "WITH", "PRAGMA")
LOGGERS = {"log", "logger", "logging"}


def _text(node: ast.AST) -> str | None:
    """A string literal's text. Each interpolated value counts as one word."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) else "X" for part in node.values
        )
    return None


def _skipped_nodes(tree: ast.AST, path: str) -> set[ast.AST]:
    skipped = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skipped.add(first.value)
        elif isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                    and func.value.id in LOGGERS):
                for arg in node.args:
                    skipped.update(ast.walk(arg))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and (path, t.id) in OWNER_VOICE for t in targets):
                skipped.update(ast.walk(node.value))
        elif isinstance(node, ast.JoinedStr):
            # An f-string's pieces are judged as part of the whole f-string.
            skipped.update(node.values)
    return skipped


def long_strings(max_words: int = MAX_MESSAGE_WORDS) -> list[str]:
    """Every scanned string over `max_words` words, as "path:line (n words): start"."""
    paths = [REPO / "bot.py"]
    for directory in SCANNED_DIRECTORIES:
        paths += sorted((REPO / directory).glob("*.py"))
    found = []
    for path in paths:
        relative = path.relative_to(REPO).as_posix()
        if relative in SKIPPED_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skipped = _skipped_nodes(tree, relative)
        for node in ast.walk(tree):
            if node in skipped:
                continue
            text = _text(node)
            if text is None or text.lstrip().startswith(SQL_KEYWORDS):
                continue
            words = word_count(text)
            if words > max_words:
                start = " ".join(text.split())[:60]
                found.append(f"{relative}:{node.lineno} ({words} words): {start}...")
    return found


class PlayerTextLengthTests(unittest.TestCase):
    def test_no_message_runs_long(self):
        found = long_strings()
        self.assertEqual(
            found, [],
            f"Over {MAX_MESSAGE_WORDS} words - cut them (CLAUDE.md, \"Writing text "
            f"players read\"):\n" + "\n".join(found),
        )

    def test_the_scan_finds_player_text_at_all(self):
        """Guards the guard: if the walk ever stopped reaching the code - a
        moved directory, a filter that catches everything - the test above
        would pass while checking nothing."""
        self.assertGreater(len(long_strings(max_words=15)), 20)

    def test_the_owners_announcements_are_skipped_not_missed(self):
        """GLOBAL_NOTICES is long on purpose; this shows the exemption is what
        keeps it out of the test above, not the scan failing to read it."""
        notices = REPO / "data" / "notifications.py"
        tree = ast.parse(notices.read_text(encoding="utf-8"))
        unfiltered = [
            node for node in ast.walk(tree)
            if (_text(node) or "") and word_count(_text(node)) > MAX_MESSAGE_WORDS
        ]
        self.assertTrue(unfiltered)


if __name__ == "__main__":
    unittest.main()
