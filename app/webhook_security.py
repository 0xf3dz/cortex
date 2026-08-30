import hashlib
import hmac


_SIGNATURE_PREFIX = "sha256="


def has_valid_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    if signature_header is None or not signature_header.startswith(_SIGNATURE_PREFIX):
        return False

    supplied_digest = signature_header.removeprefix(_SIGNATURE_PREFIX)
    if len(supplied_digest) != hashlib.sha256().digest_size * 2:
        return False

    expected_digest = hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(supplied_digest, expected_digest)
