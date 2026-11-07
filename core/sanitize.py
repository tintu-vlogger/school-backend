import re


def sanitize_string(value: str) -> str:
    """Remove potential XSS/injection patterns from strings."""
    if not isinstance(value, str):
        return value
    # Remove script tags
    value = re.sub(r'<script[^>]*>.*?</script>', '', value, flags=re.IGNORECASE | re.DOTALL)
    # Remove HTML tags
    value = re.sub(r'<[^>]+>', '', value)
    # Remove SQL injection patterns
    value = re.sub(r"[;'\"\\]", '', value)
    return value.strip()
