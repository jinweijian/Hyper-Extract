import json
from pathlib import Path

from hyperextract.documents import (
    document_package_fingerprint,
    validate_document_package,
)


FIXTURES_ROOT = Path(__file__).parent / "fixtures"
PACKAGE_ROOT = FIXTURES_ROOT / "document-package-v1.1"
FINGERPRINT_FIXTURE = FIXTURES_ROOT / "document-package-fingerprint-v1.fixture.json"


def test_shared_document_package_fixture_is_valid_and_has_stable_fingerprint():
    expected = json.loads(FINGERPRINT_FIXTURE.read_text(encoding="utf-8"))

    package = validate_document_package(PACKAGE_ROOT)

    assert expected == {
        "schema_name": "HyperExtractDocumentPackageFingerprintFixture",
        "schema_version": "1.0",
        "document_package_version": "1.1",
        "algorithm": "sha256-canonical-json",
        "fingerprint": "3a61a6d6785f0c88cfd14420625fa2988757a09364053d516be39c86f053258c",
    }
    assert package.manifest.schema_version == expected["document_package_version"]
    assert document_package_fingerprint(PACKAGE_ROOT) == expected["fingerprint"]
