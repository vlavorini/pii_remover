"""Checksum validators: the model-independent ground truth of the pipeline."""
from app.processing import validators


def test_luhn_accepts_valid_card():
    assert validators.luhn_ok("4111 1111 1111 1111")
    assert validators.luhn_ok("5500005555555559")


def test_luhn_rejects_invalid_card():
    assert not validators.luhn_ok("4111 1111 1111 1112")
    assert not validators.luhn_ok("1234")


def test_iban_mod97():
    assert validators.iban_ok("IT60X0542811101000000123456")
    assert validators.iban_ok("DE89370400440532013000")
    assert not validators.iban_ok("IT60X0542811101000000123457")


def test_italian_fiscal_code_shape_and_checksum():
    # shape is authoritative (recall first); the checksum is an extra signal
    assert validators.italian_fiscal_code_ok("RSSMRA85C14F205Z") is True
    assert validators.italian_fiscal_code_ok("not a fiscal code") is False
    assert validators.italian_fiscal_code_checksum_ok("RSSMRA85C14F205Z") is False


def test_scan_reports_fiscal_code_even_with_bad_check_char():
    spans = validators.scan("Codice Fiscale: RSSMRA85C14F205Z")
    assert any(s.kind == "TAX_ID" for s in spans)


def test_nif_es():
    assert validators.nif_es_ok("12345678Z")
    assert not validators.nif_es_ok("12345678A")


def test_ipv4_bounds():
    assert validators.ipv4_ok("192.168.1.1")
    assert not validators.ipv4_ok("999.1.1.1")


def test_scan_finds_structured_pii(sample_cv_text):
    found = {s.kind: s.value for s in validators.scan(sample_cv_text)}
    assert found["EMAIL"] == "mario.rossi@example.com"
    assert "IBAN" in found
    assert found["IBAN"] == "IT60X0542811101000000123456"
    assert "PHONE" in found


def test_scan_has_no_overlaps(sample_bank_text):
    spans = validators.scan(sample_bank_text)
    for i, a in enumerate(spans):
        for b in spans[i + 1:]:
            assert a.end <= b.start or b.end <= a.start


def test_normalize_kind_maps_aliases():
    assert validators.normalize_kind("full name") == "PERSON"
    assert validators.normalize_kind("E-Mail") == "EMAIL"
    assert validators.normalize_kind("PAN") == "CREDIT_CARD"
    assert validators.normalize_kind("nonsense-label") == "OTHER"
