"""The Render deployment files (Dockerfile, render.yaml, .dockerignore) and their documentation stay consistent with the code."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from app.core.config import load_settings

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (ROOT / ".dockerignore").read_text(encoding="utf-8")
BLUEPRINT = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
DOC = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")
SERVICE = BLUEPRINT["services"][0]
ENV = {item["key"]: item for item in SERVICE["envVars"]}


def test_there_is_one_docker_web_service_with_a_health_check():
    assert len(BLUEPRINT["services"]) == 1 and SERVICE["type"] == "web" and SERVICE["runtime"] == "docker"
    assert SERVICE["plan"] in {"free", "starter"}                               # free sleeps after inactivity; starter stays awake
    assert SERVICE.get("numInstances", 1) == 1                                  # jobs and rate-limit counters are per process
    assert SERVICE["healthCheckPath"] == "/health"
    assert SERVICE["dockerfilePath"] == "./Dockerfile" and (ROOT / "Dockerfile").is_file()


def test_the_health_route_exists(client):
    assert client.get(SERVICE["healthCheckPath"]).status_code == 200


def test_the_api_key_is_a_dashboard_secret_never_a_value_in_the_repository():
    key = ENV["GEMINI_API_KEY"]
    assert key.get("sync") is False and "value" not in key
    everything = DOCKERFILE + DOCKERIGNORE + (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "AIza" not in everything and not re.search(r"GEMINI_API_KEY\s*[=:]\s*\S{8,}", DOCKERFILE)


def test_every_variable_in_the_blueprint_is_a_real_setting_with_a_valid_value():
    known = set(re.findall(r"^([A-Z_]+)=", ENV_EXAMPLE, re.M))
    for name, item in ENV.items():
        assert name in known, name
    values = {name: str(item["value"]) for name, item in ENV.items() if "value" in item}
    settings = load_settings({**values, "GEMINI_API_KEY": "x"})            # raises on a malformed value
    assert settings.app_env == "production" and settings.docs_enabled is False
    assert settings.trusted_proxy_ips and all(p.split("/")[0].split(".")[0] in {"10", "172", "192", "100"} for p in settings.trusted_proxy_ips)
    assert settings.gemini_model == values["GEMINI_MODEL"]


def test_the_trusted_proxy_ranges_are_private_never_the_whole_internet():
    for entry in str(ENV["TRUSTED_PROXY_IPS"]["value"]).split(","):
        assert entry.strip() not in {"0.0.0.0/0", "*", "::/0"}


def test_the_image_has_ffprobe_runs_unprivileged_and_listens_on_the_port_render_gives():
    assert "apt-get install" in DOCKERFILE and "ffmpeg" in DOCKERFILE                    # ffprobe ships with the ffmpeg package
    assert re.search(r"^USER\s+(?!root)\S+", DOCKERFILE, re.M)
    assert "uvicorn app.main:app" in DOCKERFILE and "--host 0.0.0.0" in DOCKERFILE and "${PORT:-10000}" in DOCKERFILE
    assert "--no-proxy-headers" in DOCKERFILE and "--reload" not in DOCKERFILE
    assert "APP_ENV=production" in DOCKERFILE and "FROM python:3.12" in DOCKERFILE


def test_the_image_copies_only_what_it_needs_and_never_the_env_file():
    copies = re.findall(r"^COPY\s+(.+)$", DOCKERFILE, re.M)
    assert {c.split()[0] for c in copies} == {"requirements.txt", "app", "frontend", "server.py"}
    assert not any(".env" in c for c in copies)
    for pattern in (".env", ".env.*", ".venv/", "tests/", "legacy/"):
        assert pattern in DOCKERIGNORE.splitlines(), pattern


def test_the_deployment_guide_explains_the_always_on_plan_and_the_honest_limits():
    lowered = DOC.lower().replace("*", "")
    for needle in ("free", "15 minutes", "starter", "numinstances", "in memory", "deployed or built", "gemini quota",
                   "trusted_proxy_ips", "private", "ffprobe", "/health"):
        assert needle in lowered, needle
    assert "pinger" in lowered and "sleeps" in lowered                                   # honest about the free plan and keep-alive pings
    assert "docs/deployment.md" in (ROOT / "README.md").read_text(encoding="utf-8")
