"""Command classes for the action queue.

Priority constants
------------------
STOP_TASK_PRIORITY  : lower number = higher urgency (dequeued first in a PriorityQueue)
NORMAL_TASK_PRIORITY: standard operations
"""

STOP_TASK_PRIORITY   = 5
NORMAL_TASK_PRIORITY = 10


class ProtoCommand(object):
    def __init__(self):
        self.pin       = None
        self.dt        = -1
        self.priority  = 100
        self.name      = "GenCom"
        self.direction = None   # "open" | "close" | None

    def __repr__(self):
        return "C:{NAME}|P:{PIN}|dt:{DT}|Pr:{PRI}".format(
            NAME=self.name,
            DT=self.dt,
            PRI=self.priority,
            PIN=self.pin)

    def __lt__(self, other):
        """Enable priority-queue ordering."""
        return self.priority < other.priority


class StandardCommand(ProtoCommand):
    """Timed GPIO activation for open/close motor operations."""

    def __init__(self, pin, dt, direction):
        """
        Parameters
        ----------
        pin       : BCM GPIO pin number
        dt        : pulse duration in seconds (upper bound for manual commands)
        direction : "open" or "close"
        """
        super(StandardCommand, self).__init__()
        self.pin                = pin
        self.dt                 = dt
        self.direction          = direction
        self.priority           = NORMAL_TASK_PRIORITY
        self.name               = "CMD"
        self.tent_control       = None   # back-reference; set by enqueue_dt()
        self.override_was_active = False  # snapshot of override_mode at enqueue time
        self.is_manual          = False   # True for hold-to-run manual commands


# Keep the old name as an alias so tests.py continues to work unchanged.
StandardCommad = StandardCommand


class WaitingBlock(ProtoCommand):
    """No-op heartbeat that keeps the queue consumer alive."""

    def __init__(self, dt):
        super(WaitingBlock, self).__init__()
        self.dt       = dt
        self.priority = NORMAL_TASK_PRIORITY
        self.name     = "WAIT"


class StopBlockEnd(ProtoCommand):
    def __init__(self, stop_block_uuid):
        super(StopBlockEnd, self).__init__()
        self.stop_block_uuid = stop_block_uuid
        self.priority        = NORMAL_TASK_PRIORITY
        self.name            = "STOP_END"


class StopBlock(ProtoCommand):
    def __init__(self, uuid_str):
        super(StopBlock, self).__init__()
        self.priority      = STOP_TASK_PRIORITY
        self.name          = "STOP"
        self.my_uuid       = uuid_str
        self.BlockEndPair  = StopBlockEnd(self.my_uuid)

    def getBlockEndPair(self):
        return self.BlockEndPair
