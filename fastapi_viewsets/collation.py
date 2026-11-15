"""
Text collations of the in-memory sort and cursor.

A collation is any object with a `sort_key(text)` method. `get_collation()` returns the default
collation or, for a locale, an ICU collation (the `icu` extra).
"""

import re
import threading
import warnings

from typing import Any, Literal, Protocol, runtime_checkable

OnMismatch = Literal["use", "warn", "fallback"]

# At most 640 digits per run, the lowest limit `sys.set_int_max_str_digits` accepts, so `int()` never refuses one;
# a longer run compares as consecutive 640-digit runs.
_DIGIT_RUN = re.compile(r"(\d{1,640})")


@runtime_checkable
class Collation(Protocol):
    """A text order: strings compare by their `sort_key` values."""

    def sort_key(self, text: str) -> Any:
        """The key of `text`. Keys of one collation compare with each other."""
        ...


class CollationWarning(UserWarning):
    """`get_collation()` orders text in another order than the requested locale's."""


class _DefaultCollation:
    """Case-insensitive, with digit runs compared by value; the string itself breaks ties."""

    __slots__ = ()

    def sort_key(self, text: str) -> tuple[tuple, str]:
        parts: list[Any] = _DIGIT_RUN.split(text)
        parts[0::2] = map(str.casefold, parts[0::2])
        parts[1::2] = map(int, parts[1::2])
        return tuple(parts), text

    def __repr__(self) -> str:
        return "<default collation>"


class _IcuCollation:
    """An ICU collator of `locale` with numeric collation on; the string itself breaks ties."""

    def __init__(self, icu: Any, locale: str, icu_locale: Any) -> None:
        self._icu = icu
        self.locale = locale
        self._icu_locale = icu_locale
        self._threads = threading.local()
        self._collator = self._threads.collator = self._create()

    def _create(self) -> Any:
        collator = self._icu.Collator.createInstance(self._icu_locale)
        collator.setAttribute(self._icu.UCollAttribute.NUMERIC_COLLATION, self._icu.UCollAttributeValue.ON)
        return collator

    def sort_key(self, text: str) -> tuple[bytes, str]:
        # One collator per thread: no ICU collator instance is used by two threads at once.
        try:
            collator = self._threads.collator
        except AttributeError:
            collator = self._threads.collator = self._create()
        return collator.getSortKey(text), text

    def __repr__(self) -> str:
        return f"<ICU collation {self.locale!r}>"


_DEFAULT = _DefaultCollation()
_collations: dict[tuple[str, str], Collation] = {}
_lock = threading.Lock()


def get_collation(locale: str | None = None, on_mismatch: OnMismatch = "warn") -> Collation:
    """
    The collation of `locale`. None returns the default collation: case-insensitive, digit runs by value.
    A locale returns an ICU collator of its canonical form (`iw` is `he`) with numeric collation on (the
    `icu` extra); without PyICU it returns the default collation and warns. A mismatch is ICU loading its
    root collation for a locale whose language is not root. On a mismatch `on_mismatch` decides: `use` uses
    ICU's collation silently, `warn` uses it and warns, `fallback` warns and returns the default collation.
    Each `(locale, on_mismatch)` is created and checked once, and its `CollationWarning` is issued then;
    later calls return the same instance. Raises `ValueError` for a string ICU does not read as a locale.
    """
    if on_mismatch not in ("use", "warn", "fallback"):
        raise ValueError(f"on_mismatch {on_mismatch!r} is not 'use', 'warn' or 'fallback'")
    if locale is None:
        return _DEFAULT
    key = (locale, on_mismatch)
    collation = _collations.get(key)
    if collation is None:
        with _lock:
            collation = _collations.get(key)
            if collation is None:
                collation = _collations[key] = _create(locale, on_mismatch)
    return collation


def _create(locale: str, on_mismatch: OnMismatch) -> Collation:
    try:
        import icu
    except ImportError:
        warnings.warn(f"PyICU is not installed: collation {locale!r} is the default", CollationWarning, stacklevel=3)
        return _DEFAULT
    try:
        canonical = icu.Locale.createCanonical(locale)
    except icu.ICUError as exc:
        raise ValueError(f"{locale!r} is not a locale") from exc
    language = canonical.getLanguage()
    if not language and not _names_root(locale):
        raise ValueError(f"{locale!r} is not a locale")
    collation = _IcuCollation(icu, locale, canonical)
    # The valid locale is the one ICU has collation data for, own or inherited; root ("") when it has none.
    valid = str(collation._collator.getLocale(icu.ULocDataLocaleType.VALID_LOCALE))
    if on_mismatch == "use" or valid or not language:
        return collation
    if on_mismatch == "fallback":
        message = f"ICU has no collation for {locale!r}: collation {locale!r} is the default"
        warnings.warn(message, CollationWarning, stacklevel=3)
        return _DEFAULT
    message = f"ICU has no collation for {locale!r}: collation {locale!r} is the ICU root collation"
    warnings.warn(message, CollationWarning, stacklevel=3)
    return collation


def _names_root(locale: str) -> bool:
    """Whether `locale` requests ICU's root locale: empty, or language `root` or `und`."""
    return locale == "" or re.split(r"[-_@]", locale, maxsplit=1)[0].lower() in ("root", "und")
