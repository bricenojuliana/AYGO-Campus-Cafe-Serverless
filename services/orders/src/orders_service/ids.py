"""Human-friendly order codes such as CC-7K3Q-9XMD.

Crockford base32 (no I, L, O, U) keeps codes readable aloud and hard to mistype. Eight random
characters give 40 bits; uniqueness is still enforced by the conditional write, and the service
retries with a new code on the rare collision.
"""
import re
import secrets

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PREFIX = "CC"
ORDER_CODE_PATTERN = re.compile(r"^CC-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$")
# Characters people commonly type for the ones Crockford base32 leaves out.
_CONFUSABLES = str.maketrans({"O": "0", "I": "1", "L": "1"})


def new_order_id() -> str:
    chars = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"{PREFIX}-{chars[:4]}-{chars[4:]}"


def normalize_order_id(raw: str) -> str:
    """Accepts 'cc-7k3q-9xmd', 'CC7K3Q9XMD' or ' CC-7K3Q-9XMD '; returns the canonical form or ''."""
    if not isinstance(raw, str):
        return ""
    compact = re.sub(r"[\s-]", "", raw).upper()
    if not compact.startswith(PREFIX) or len(compact) != 10:
        return ""
    body = compact[2:].translate(_CONFUSABLES)
    candidate = f"{PREFIX}-{body[:4]}-{body[4:]}"
    return candidate if ORDER_CODE_PATTERN.match(candidate) else ""
