"""M14: FSM-scaffolded retain pipeline.

See ``docs/_design/m13-m14-roadmap.md`` §4 for the full design and
phase plan.

M14.0 (this commit) ships the engine + context + checkpoint scaffold
plus three stub states (``INIT``, ``READ``, ``COMPLETE``). M14.1
through M14.5 fill in the real extraction / compile / wiki / supersedes
states.

Public API::

    from astrocyte.pipeline.retain_fsm import (
        RetainFSM, RetainContext, RetainServices,
        Complete, Failed, Parallel,
        FileCheckpoint, InMemoryCheckpoint,
        register_default_states,
    )

Usage::

    services = RetainServices(store=..., provider=...)
    fsm = RetainFSM(services)
    register_default_states(fsm)
    ctx = RetainContext(bank_id="b1", source_id="s1", md_text="...")
    ctx = await fsm.run(ctx, checkpoint=FileCheckpoint("./checkpoints"))
"""

from astrocyte.pipeline.retain_fsm.checkpoint import (
    Checkpoint,
    FileCheckpoint,
    InMemoryCheckpoint,
)
from astrocyte.pipeline.retain_fsm.context import (
    RetainContext,
    RetainServices,
    StepLogEntry,
)
from astrocyte.pipeline.retain_fsm.engine import (
    Complete,
    Failed,
    Parallel,
    RetainFSM,
    StateFunc,
)
from astrocyte.pipeline.retain_fsm.states import (
    DEFAULT_STATES,
    register_default_states,
    state_complete,
    state_init,
    state_read,
)

__all__ = [
    "DEFAULT_STATES",
    "Checkpoint",
    "Complete",
    "Failed",
    "FileCheckpoint",
    "InMemoryCheckpoint",
    "Parallel",
    "RetainContext",
    "RetainFSM",
    "RetainServices",
    "StateFunc",
    "StepLogEntry",
    "register_default_states",
    "state_complete",
    "state_init",
    "state_read",
]
