"""CSV writer that keeps typed-in text from running as a spreadsheet formula.

Excel and Sheets treat a cell starting with = + - @ (or a tab/CR) as a formula, so a
name entered as =HYPERLINK(...) would become a live link in the company's export.
Such cells get a leading apostrophe, which spreadsheets hide.
"""
import csv

_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _clean(value):
    if isinstance(value, str) and value.startswith(_TRIGGERS):
        return "'" + value
    return value


class _SafeWriter:
    def __init__(self, f, **kw):
        self._w = csv.writer(f, **kw)

    def writerow(self, row):
        return self._w.writerow([_clean(v) for v in row])

    def writerows(self, rows):
        for row in rows:
            self.writerow(row)


def writer(f, **kw) -> _SafeWriter:
    return _SafeWriter(f, **kw)
