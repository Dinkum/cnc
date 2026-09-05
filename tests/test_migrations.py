from pathlib import Path
import re


REVISION_PATTERN = re.compile(r'^revision = "([^"]+)"$', re.MULTILINE)
DOWN_REVISION_PATTERN = re.compile(r'^down_revision = ("([^"]+)"|None)$', re.MULTILINE)


def test_migration_chain_references_only_present_revisions() -> None:
    versions_dir = Path("migrations/versions")
    revision_ids: set[str] = set()
    down_revisions: list[str] = []

    for path in sorted(versions_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8")
        revision_match = REVISION_PATTERN.search(text)
        down_revision_match = DOWN_REVISION_PATTERN.search(text)
        assert revision_match is not None, f"missing revision in {path}"
        assert down_revision_match is not None, f"missing down_revision in {path}"
        revision_ids.add(revision_match.group(1))
        if down_revision_match.group(2):
            down_revisions.append(down_revision_match.group(2))

    for down_revision in down_revisions:
        assert down_revision in revision_ids, (
            f"missing migration for down_revision {down_revision}"
        )
