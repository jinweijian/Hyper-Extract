from hyperextract.cli.config import ConfigManager


def test_named_llm_profile_reads_provider_neutral_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPER_EXTRACT_LLM_PROFILE", "FAST_ROUTE")
    monkeypatch.setenv("FAST_ROUTE_MODEL", "fast-model")
    monkeypatch.setenv("FAST_ROUTE_API_KEY", "secret")
    monkeypatch.setenv("FAST_ROUTE_BASE_URL", "https://example.test/v1")

    config = ConfigManager(tmp_path / "missing.toml").get_llm_config()

    assert config.model == "fast-model"
    assert config.api_key == "secret"
    assert config.base_url == "https://example.test/v1"


def test_embedding_environment_does_not_inherit_llm_route(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPER_EXTRACT_LLM_MODEL", "chat-model")
    monkeypatch.setenv("HYPER_EXTRACT_LLM_API_KEY", "chat-key")
    monkeypatch.setenv("HYPER_EXTRACT_LLM_BASE_URL", "https://chat.test/v1")
    monkeypatch.setenv("HYPER_EXTRACT_EMBEDDING_MODEL", "embedding-model")
    monkeypatch.setenv("HYPER_EXTRACT_EMBEDDING_API_KEY", "embedding-key")
    monkeypatch.setenv("HYPER_EXTRACT_EMBEDDING_BASE_URL", "https://embedding.test/v1")

    manager = ConfigManager(tmp_path / "missing.toml")
    llm = manager.get_llm_config()
    embedding = manager.get_embedder_config()

    assert (llm.model, llm.api_key, llm.base_url) == (
        "chat-model",
        "chat-key",
        "https://chat.test/v1",
    )
    assert (embedding.model, embedding.api_key, embedding.base_url) == (
        "embedding-model",
        "embedding-key",
        "https://embedding.test/v1",
    )
