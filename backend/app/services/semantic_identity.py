"""Conservative identity keys, never OCR correction or semantic equivalence.

Scientific tokens and executable-language text must not inherit prose folding.
No input code is executed. A missed merge is preferable to a false equivalence.
"""
import re
import unicodedata


SENSITIVE_DOMAINS = {'programming', 'python', 'organic_chemistry', 'chemistry',
                     'probability', 'mathematics', 'math', 'statistics'}


def meaning_key(value: str) -> str:
    # Keep case, indentation, string-literal spaces, Unicode symbol distinctions
    # and formula conditions. NFC does not fold subscripts/fullwidth characters.
    return unicodedata.normalize('NFC', value).strip()


def name_key(value: str, domain: str) -> str:
    value = meaning_key(value)
    if domain in SENSITIVE_DOMAINS or re.search(r'[=(){}\\_^+*/<>\d]', value):
        return value
    # Single symbols can denote distinct variables/elements even in an
    # otherwise natural-language course. Keep their case in unknown domains.
    if len(value) <= 2 and value.isascii():
        return value
    return re.sub(r'\s+', '', value).casefold()
