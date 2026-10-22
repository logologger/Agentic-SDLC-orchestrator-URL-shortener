"""Short-code generation using a CSPRNG (codes must not be guessable/enumerable)."""
import secrets
import string

ALPHABET = string.ascii_letters + string.digits
CODE_LENGTH = 7  # 62**7 ~= 3.5e12 codes


def generate_code(length: int = CODE_LENGTH) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))
