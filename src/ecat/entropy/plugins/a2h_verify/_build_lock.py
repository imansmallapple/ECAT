"""One lock around every hvigor build that ECAT itself starts during an evaluation.

The engine runs hard checks in parallel threads. On dynamic iterations the compile
plugin and the device session (runtime_liveness / ui_align) both run
``hvigorw assembleHap`` in the same HarmonyOS project at the same moment. Two hvigor
processes in one project race on ``build/`` and ``.hvigor/`` (e.g. "Failed to create
the directory or file ... File exists" in CompileResource, or ENOENT on
``.hvigor/report/*.json``) and one of them fails at random: either the dynamic
dimensions are skipped, or ``compile`` is wrongly scored as a broken build.
Holding this lock around each build makes the two builds run one after the other.
"""

from __future__ import annotations

import threading

HVIGOR_BUILD_LOCK = threading.Lock()
