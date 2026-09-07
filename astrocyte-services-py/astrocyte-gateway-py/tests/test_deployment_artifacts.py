"""Guards the gateway's shipped deployment artifacts.

Scope note, because this is deliberately *narrower* than the AML adapter's
equivalent (`astrocyte-aml-py/tests/test_deployment_path.py`). That package had
a real hole: every test injected a brain, so the container's construction path
was never executed and shipped broken. **The gateway does not have that hole** —
39 call sites already use bare ``create_app()``, which routes through
``build_astrocyte()``, so the deployed wiring is well covered. Duplicating those
tests here would add runtime and catch nothing.

What is *not* covered is everything outside the Python process: no test reads
either Dockerfile or ``config.example.yaml``. Those are the artifacts an
operator runs, and they can drift from the code without a single test going
red — an image that builds fine and then exits immediately because its ``CMD``
names an entry point that was renamed. That is the gap these fill.
"""

from __future__ import annotations

import importlib
import re
import tomllib
from pathlib import Path

import pytest
import yaml

PKG_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = PKG_ROOT / "Dockerfile"
DOCKERFILE_RELEASE = PKG_ROOT / "Dockerfile.release"
EXAMPLE_CONFIG = PKG_ROOT / "config.example.yaml"
PYPROJECT = PKG_ROOT / "pyproject.toml"


def dockerfiles() -> list[Path]:
    return [p for p in (DOCKERFILE, DOCKERFILE_RELEASE) if p.exists()]


def executable_lines(path: Path) -> str:
    """Dockerfile with comments stripped.

    Matching raw text is a false-negative trap: a comment explaining why a
    dependency is needed contains the same marker as the install itself, so a
    deleted install still looks present. Only instructions count.
    """
    return "\n".join(
        ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#")
    )


class TestDeclaredEntrypointsExist:
    """Each image's ``CMD`` must name something that actually exists.

    This is the gateway's version of the AML failure mode: the image builds,
    starts, and dies immediately. A renamed console script or a deleted
    ``__main__.py`` is invisible to every other test in this suite.
    """

    def test_console_script_is_declared_in_pyproject(self):
        scripts = tomllib.loads(PYPROJECT.read_text())["project"]["scripts"]
        assert "astrocyte-gateway-py" in scripts

    def test_console_script_target_is_importable(self):
        scripts = tomllib.loads(PYPROJECT.read_text())["project"]["scripts"]
        module_path, _, func = scripts["astrocyte-gateway-py"].partition(":")
        module = importlib.import_module(module_path)
        assert callable(getattr(module, func)), f"{module_path}:{func} is not callable"

    def test_module_entrypoint_exists_for_the_release_image(self):
        # Dockerfile.release runs `-m astrocyte_gateway`, which needs __main__.
        assert (PKG_ROOT / "astrocyte_gateway" / "__main__.py").exists()

    def test_every_image_cmd_matches_a_real_entrypoint(self):
        scripts = tomllib.loads(PYPROJECT.read_text())["project"]["scripts"]
        for path in dockerfiles():
            cmd = re.search(r"^CMD\s+(\[.*\])", executable_lines(path), re.MULTILINE)
            assert cmd, f"{path.name} declares no CMD"
            tokens = re.findall(r'"([^"]+)"', cmd.group(1))
            if "-m" in tokens:
                module = tokens[tokens.index("-m") + 1]
                assert (PKG_ROOT / module.replace(".", "/") / "__main__.py").exists(), (
                    f"{path.name} runs `-m {module}` but that module has no __main__.py"
                )
            else:
                assert tokens[0] in scripts, (
                    f"{path.name} CMD {tokens[0]!r} is not a declared console script"
                )


class TestShippedExampleConfigIsUsable:
    """`config.example.yaml` is what a new operator copies first."""

    def test_it_parses(self):
        assert yaml.safe_load(EXAMPLE_CONFIG.read_text()), "example config must be non-empty YAML"

    def test_it_builds_a_wired_brain(self, monkeypatch):
        """The whole point of the AML bug: config that loads but yields no pipeline."""
        from astrocyte_gateway.brain import build_astrocyte

        monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(EXAMPLE_CONFIG))
        brain = build_astrocyte()
        assert brain._pipeline is not None, "example config produced a brain with no pipeline"

    def test_it_names_only_resolvable_providers(self, monkeypatch):
        from astrocyte._discovery import resolve_provider

        cfg = yaml.safe_load(EXAMPLE_CONFIG.read_text())
        for key, group in (
            ("vector_store", "vector_stores"),
            ("llm_provider", "llm_providers"),
            ("embedding_provider", "llm_providers"),
        ):
            if name := cfg.get(key):
                assert resolve_provider(name, group) is not None, f"{key}={name!r} does not resolve"


class TestImagesAgree:
    """Two Dockerfiles is two chances to drift."""

    def test_both_images_expose_the_same_port(self):
        ports = {}
        for path in dockerfiles():
            match = re.search(r"^EXPOSE\s+(\d+)", executable_lines(path), re.MULTILINE)
            assert match, f"{path.name} declares no EXPOSE"
            ports[path.name] = match.group(1)
        assert len(set(ports.values())) == 1, f"images disagree on the exposed port: {ports}"

    def test_dev_image_builds_the_library_from_local_source(self):
        """The dev image must NOT install a published wheel.

        Resolving astrocyte from PyPI instead of the checkout is how the
        provider registry silently goes stale — the entry points shipped in the
        last release win over the code under test (the uv.lock trap).
        """
        body = executable_lines(DOCKERFILE)
        # Assert the actual install instruction, not merely that the string
        # appears somewhere: a bare substring check survives deleting the COPY,
        # because the pip line mentions the same path.
        installs_local = re.search(r"pip install[^\n]*/build/astrocyte-py", body)
        assert installs_local, "Dockerfile must pip-install the copied local astrocyte-py"
        pypi_pin = re.search(r"pip install[^\n]*[\"']?astrocyte==", body)
        assert not pypi_pin, "dev image must not install astrocyte from PyPI"

    def test_release_image_pins_an_exact_version(self):
        """The release image installs from PyPI *by design* — so pin it.

        Deliberately different from the dev image: it installs astrocyte at the
        version matching the git tag. That is correct for a release, but only
        while the version is an explicit build arg; an unpinned install would
        float to whatever is newest on PyPI.
        """
        body = executable_lines(DOCKERFILE_RELEASE)
        # The pin must be on the install spec itself. Checking only that the
        # token appears somewhere passes even when the ARG is renamed, because
        # the same name occurs on four other lines.
        assert re.search(r"astrocyte==\$\{ASTROCYTE_VERSION\}", body), (
            "release image must install astrocyte==${ASTROCYTE_VERSION}, not an unpinned spec"
        )
        assert re.search(r"^ARG\s+ASTROCYTE_VERSION", body, re.MULTILINE), (
            "ASTROCYTE_VERSION must be a declared build arg"
        )
        assert "SETUPTOOLS_SCM_PRETEND_VERSION" in body, (
            "release image needs a pretend-version: the build context carries no .git"
        )

    @pytest.mark.parametrize("path", [p.name for p in dockerfiles()])
    def test_build_context_expectation_is_documented(self, path):
        """Both images COPY sibling packages, so the context is the repo root.

        A context rooted at this package cannot see ``astrocyte-py`` and the
        build fails at install time — the same footgun the AML image had.
        """
        body = executable_lines(PKG_ROOT / path)
        copies_siblings = any(
            marker in body
            for marker in ("astrocyte-py", "adapters-storage-py", "astrocyte-services-py/")
        )
        assert copies_siblings, f"{path} does not appear to build from the repo root"
