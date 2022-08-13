"""elements for the command queue to replace the dictoriaries and enable 
a priority queue structure.
"""

STOP_TASK_PRIORITY = 5
NORMAL_TASK_PRIORITY = 10


class ProtoCommand(object):
    def __init__(self):
        self.pin = None
        self.dt = -1
        self.priority = 100
        self.name = "GenCom"

    def __repr__(self):
        return "C:{NAME}|P:{PIN}|dt:{DT}|Pr:{PRI}".format(
            NAME=self.name,
            DT=self.dt,
            PRI=self.priority,
            PIN=self.pin)
              

    def __lt__(self, other):
        """to allow priority queues"""
        return self.priority < other.priority


class StandardCommad(ProtoCommand):
    """For open/close operations.
    """
    
    def __init__(self, command_dict):
        """ Initialized with pin and time
        """
        super(StandardCommad, self).__init__()
        
        self.pin = command_dict["pin"]
        self.dt = command_dict["dt"]
        self.priority = NORMAL_TASK_PRIORITY


class WaitingBlock(ProtoCommand):
    def __init__(self,dt):
        super(WaitingBlock, self).__init__()
        
        self.dt = dt
        self.priority = NORMAL_TASK_PRIORITY


class StopBlockEnd(ProtoCommand):
    def __init__(self,stop_block_uuid):
        super(StopBlockEnd, self).__init__()
        self.stop_block_uuid = stop_block_uuid
        self.priority = NORMAL_TASK_PRIORITY

        
class StopBlock(ProtoCommand):
    def __init__(self):
        super(StopBlock, self).__init__()
        self.priority = STOP_TASK_PRIORITY
        self.name = "STOP"
        self.my_uuid = "UUID"  # todo generate uuid here

        self.BlockEndPair = StopBlockEnd(self.my_uuid)

    def getBlockEndPair(self):
        return self.BlockEndPair
        


