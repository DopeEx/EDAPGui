"""Extract one dated release section without including the legacy changelog history."""

import argparse
import re
from pathlib import Path


def extract_release_notes(changelog: str, version: str) -> str:
    heading = re.compile(rf"## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}")
    found = False
    lines = []
    for line in changelog.splitlines():
        if found and re.match(r"^#{1,2} ", line):
            break
        if found:
            lines.append(line)
        elif heading.fullmatch(line):
            found = True
    notes = "\n".join(lines).strip()
    if not notes:
        raise ValueError(f"No non-empty dated changelog section for version {version}.")
    return notes


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    args = parser.parse_args()
    changelog = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    try:
        notes = extract_release_notes(changelog, args.version)
    except ValueError as error:
        parser.exit(1, f"{error}\n")
    print(notes)
