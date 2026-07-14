import pytest

from hyperextract.service.storage import SharedVolumeStore


def test_package_uri_is_contained_and_run_is_reserved(exchange_root, package_path):
    store = SharedVolumeStore(exchange_root)
    assert store.resolve_package_uri(package_path.as_uri()) == package_path.resolve()
    reserved = store.reserve_run("run_test")
    assert (reserved / "work").is_dir()
    assert store.output_locations("run_test").manifest_uri.endswith(
        "/artifact-manifest.json"
    )


def test_package_uri_rejects_protocol_escape_and_symlink(exchange_root, package_path):
    store = SharedVolumeStore(exchange_root)
    with pytest.raises(ValueError, match="MUST_USE_FILE"):
        store.resolve_package_uri("https://example.test/course.hepkg")
    with pytest.raises(ValueError, match="PATH_FORBIDDEN"):
        store.resolve_package_uri((exchange_root / "runs").as_uri())
    link = exchange_root / "packages" / "link.hepkg"
    link.symlink_to(package_path, target_is_directory=True)
    with pytest.raises(ValueError, match="PATH_FORBIDDEN"):
        store.resolve_package_uri(link.as_uri())


def test_package_uri_rejects_query_and_fragment(exchange_root, package_path):
    store = SharedVolumeStore(exchange_root)
    uri = package_path.as_uri()
    with pytest.raises(ValueError, match="DOCUMENT_PACKAGE_URI_INVALID"):
        store.resolve_package_uri(f"{uri}?revision=2")
    with pytest.raises(ValueError, match="DOCUMENT_PACKAGE_URI_INVALID"):
        store.resolve_package_uri(f"{uri}#section")
