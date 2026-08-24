"""
Guards for the external binaries this suite shells out to.

Two of them are not Python: `php`, which IS the PHP parser (there is no
pure-Python substitute — see mcp_bifrost/languages/extract.php), and `git`,
which the VCS layer drives for real in the end-to-end tests. Neither is a
Python dependency, so neither can be declared in pyproject.toml, and a fresh
clone on a machine without them used to produce 53 failures and a wall of
`FileNotFoundError` that read like broken code rather than a missing binary.

Skipping is the honest signal: without `php` the PHP half of the suite is
not proven, and saying so is better than claiming a pass it did not earn.
"""

from __future__ import annotations

import shutil
import unittest

HAS_PHP = shutil.which("php") is not None
HAS_GIT = shutil.which("git") is not None

requires_php = unittest.skipUnless(
    HAS_PHP, "requires the php CLI binary (apt install php-cli / brew install php)"
)
requires_git = unittest.skipUnless(
    HAS_GIT, "requires the git binary"
)
