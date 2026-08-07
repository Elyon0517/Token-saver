#!/usr/bin/env python3
"""Shared test helpers.

Named with a leading underscore so unittest discovery (pattern `test_*.py`) skips it.
Importable because discovery puts the tests directory on sys.path.
"""

import contextlib
import io


class QuietCliMixin:
    """Silence a CLI's stdout for the duration of a test.

    CLI tests call main(), which prints JSON by design. Without this the JSON lands in
    the middle of the test report and reads like output from a failing test.

    Mix in first so its setUp runs before the concrete case's, and cooperate with the
    rest of the MRO via super().
    """

    def setUp(self):
        super().setUp()
        redirect = contextlib.redirect_stdout(io.StringIO())
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
