"""Guards the path the container actually takes.

The bug these exist for: every other test in this package builds the app with
``create_app(brain=...)``, injecting a ready-made brain. The container does not
do that — it sets ``ASTROCYTE_CONFIG_PATH`` and lets the app construct its own.
That path was broken (``Astrocyte.from_config()`` returns a brain with no
pipeline), and 70 passing tests said nothing, because not one of them exercised
it. A suite that only covers the injected-dependency path cannot tell you the
deployed path works.

So these tests deliberately avoid injection. They drive the app the way the
Dockerfile does, and they check the shipped artifacts against each other:
a config naming a provider the image never installs is a green test suite and a
500 on the evaluator's first request.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from astrocyte_aml.app import create_app

PKG_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_CONFIG = PKG_ROOT / "deploy" / "astrocyte.aml.yaml"
DOCKERFILE = PKG_ROOT / "Dockerfile"
COMPOSE = PKG_ROOT / "docker-compose.yml"

# Config provider name -> what the image must install for it to resolve.
PROVIDER_REQUIREMENTS = {
    "postgres": "astrocyte-postgres",
    "openai": "[openai]",
    "local_embeddings": "sentence-transformers",
}

HERMETIC_CONFIG = """
provider_tier: storage
vector_store: in_memory
llm_provider: mock
access_control:
  enabled: false
"""


@pytest.fixture
def deployed_app(tmp_path, monkeypatch):
    """The app built the way the container builds it: config path, no injection."""
    cfg = tmp_path / "astrocyte.yaml"
    cfg.write_text(HERMETIC_CONFIG)
    monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
    monkeypatch.delenv("ASTROCYTE_AML_API_KEY", raising=False)
    return create_app()  # brain=None -> the deployed construction path


class TestDeployedPathServesTheContract:
    """create_app() with no injected brain must serve AML's endpoints."""

    def test_health_reports_ready(self, deployed_app):
        with TestClient(deployed_app) as client:
            assert client.get("/health").status_code == 200

    def test_add_succeeds_through_the_config_path(self, deployed_app):
        # This is the exact request that would have 500'd on the evaluator.
        with TestClient(deployed_app) as client:
            resp = client.post(
                "/add",
                json={
                    "request_id": "r1",
                    "session_id": "s1",
                    "user_id": "u1",
                    "messages": [{"role": "user", "content": "Alice owns payments."}],
                },
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] is True

    def test_search_succeeds_through_the_config_path(self, deployed_app):
        with TestClient(deployed_app) as client:
            client.post(
                "/add",
                json={
                    "request_id": "r1",
                    "session_id": "s1",
                    "user_id": "u1",
                    "messages": [{"role": "user", "content": "Alice owns payments."}],
                },
            )
            resp = client.post("/search", json={"query": "who owns payments", "user_id": "u1", "top_k": 5})
        assert resp.status_code == 200, resp.text
        # AML's Search contract returns `data`; assert the retained memory came
        # back, not merely that the endpoint answered.
        body = resp.json()
        assert "data" in body, body
        assert any("Alice owns payments" in item.get("content", "") for item in body["data"]), body

    def test_missing_config_is_reported_as_unready_not_as_healthy(self, monkeypatch):
        # The failure mode that let a broken container look green.
        monkeypatch.delenv("ASTROCYTE_CONFIG_PATH", raising=False)
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            assert client.get("/health").status_code == 503


class TestShippedConfigIsUsable:
    """`deploy/astrocyte.aml.yaml` is the artifact maintainers run."""

    def test_it_parses(self):
        assert yaml.safe_load(DEPLOY_CONFIG.read_text()), "shipped config must be non-empty YAML"

    def test_it_builds_a_pipeline(self, monkeypatch):
        """Constructs the real providers — catches drift in names and keys.

        No database or API call happens: PostgresStore connects lazily and the
        OpenAI client is not exercised, so dummy credentials are enough to prove
        the config resolves to working objects.
        """
        pytest.importorskip("openai")
        pytest.importorskip("astrocyte_postgres")
        from astrocyte.config import load_config

        from astrocyte_aml.wiring import build_pipeline

        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:5999/unused")
        monkeypatch.setenv("OPENAI_API_KEY", "dummy-not-called")

        pipeline = build_pipeline(load_config(str(DEPLOY_CONFIG)))
        inner = getattr(pipeline.llm_provider, "_inner", pipeline.llm_provider)
        assert type(pipeline.vector_store).__name__ == "PostgresStore"
        assert type(inner).__name__ == "OpenAIProvider"

    def test_it_pins_the_model_aml_mandates(self):
        """AML requires gpt-4o-mini for Add and Search — a condition of entry."""
        cfg = yaml.safe_load(DEPLOY_CONFIG.read_text())
        assert cfg["llm_provider_config"]["model"] == "gpt-4o-mini"

    def test_it_pins_the_cost_critical_flag(self):
        """parallel_chunks off is ~$230 vs ~$720 across the suite (roadmap §4d)."""
        cfg = yaml.safe_load(DEPLOY_CONFIG.read_text())
        assert cfg["structured_fact_extraction"]["parallel_chunks"] is False


class TestImageSatisfiesTheConfig:
    """The Dockerfile must install what the shipped config asks for.

    Pure text comparison — no packages required — so it runs everywhere and
    catches the divergence that produced a 503 in the real container: the
    config named `openai`, the image never installed the `[openai]` extra, and
    the failure only appeared when the brain was first built.
    """

    def _configured_providers(self) -> set[str]:
        cfg = yaml.safe_load(DEPLOY_CONFIG.read_text())
        names = {cfg.get("vector_store"), cfg.get("llm_provider"), cfg.get("embedding_provider")}
        return {n for n in names if n}

    @staticmethod
    def _executable_dockerfile() -> str:
        """Dockerfile with comments stripped.

        Matching raw text is a false-negative trap: a comment *explaining* why
        the `[openai]` extra is required also contains the marker, so removing
        the real install still looked installed. Only instructions count.
        """
        lines = [ln for ln in DOCKERFILE.read_text().splitlines() if not ln.lstrip().startswith("#")]
        return "\n".join(lines)

    def test_every_configured_provider_is_installed_by_the_image(self):
        dockerfile = self._executable_dockerfile()
        missing = [
            f"{name} (expects {marker!r} in the Dockerfile)"
            for name, marker in PROVIDER_REQUIREMENTS.items()
            if name in self._configured_providers() and marker not in dockerfile
        ]
        assert not missing, "shipped config names providers the image does not install: " + "; ".join(missing)

    def test_dockerfile_and_config_agree_on_the_config_path(self):
        dockerfile = DOCKERFILE.read_text()
        assert "ASTROCYTE_CONFIG_PATH" in dockerfile
        # The file the image copies must be the one this package ships.
        assert "astrocyte.aml.yaml" in dockerfile

    def test_compose_supplies_every_env_var_the_config_needs(self):
        compose = COMPOSE.read_text()
        for required in ("DATABASE_URL", "OPENAI_API_KEY", "ASTROCYTE_CONFIG_PATH"):
            assert required in compose, f"docker-compose.yml must set {required}"

    def test_build_context_is_the_repo_root(self):
        """The adapter resolves astrocyte from a sibling path.

        A context rooted at this package cannot see the library, so the build
        fails at install time — a footgun worth pinning.
        """
        compose = yaml.safe_load(COMPOSE.read_text())
        for service in compose["services"].values():
            build = service.get("build")
            if isinstance(build, dict) and "dockerfile" in build:
                assert build["context"] == "../..", "build context must be the repo root"

    def test_healthcheck_is_declared(self):
        """An evaluator waits on health; a container without one looks ready instantly."""
        assert re.search(r"HEALTHCHECK", DOCKERFILE.read_text())
