"""Post-generation hook: strip ``.jinja`` suffix from rendered template files.

{% raw %}
Why this exists
---------------
Cookiecutter renders Jinja2 syntax inside ``{{ cookiecutter.* }}`` placeholders.
The template files under ``{{cookiecutter.package_slug}}/`` have ``.py.jinja``
filenames rather than plain ``.py`` so that:

1. Static analyzers (CodeQL, GitHub's Standard findings, IDE Python parsers,
   ruff, etc.) don't try to parse the raw templates as Python — they
   contain Jinja placeholders that aren't valid Python until rendered.
2. The intent is obvious at the filesystem level: a ``.jinja`` suffix
   reads as "Jinja template", a bare ``.py`` reads as "valid Python".

After cookiecutter has rendered each file's contents, this hook walks the
output tree and renames ``foo.py.jinja`` → ``foo.py``. The hook runs from
the generated project's root directory.
{% endraw %}
"""

from __future__ import annotations

import os
from pathlib import Path


def main() -> None:
    project_root = Path(os.getcwd())
    for path in project_root.rglob("*.jinja"):
        target = path.with_name(path.name.removesuffix(".jinja"))
        path.rename(target)


if __name__ == "__main__":
    main()
