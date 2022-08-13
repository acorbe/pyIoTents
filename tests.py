import commands
from tornado.queues import PriorityQueue


def test_prqueue_insert():
    

    q = PriorityQueue()

    my_std_command1 = commands.StandardCommad({'pin' : 5 , 'dt' : 0.2})
    q.put(my_std_command1)
    
    my_std_command2 = commands.StandardCommad({'pin' : 6 , 'dt' : 0.3})
    q.put(my_std_command2)

    e1 = q.get_nowait()
    assert e1 == my_std_command1

    print(e1)
    print(q.get_nowait())


def test_prqueue_insertStop():
    """tests that __lt__ method is properly accepted by the priority queue"""

    q = PriorityQueue()

    my_std_command1 = commands.StandardCommad({'pin' : 5 , 'dt' : 0.2})
    q.put(my_std_command1)
    
    my_std_command2 = commands.StandardCommad({'pin' : 6 , 'dt' : 0.3})
    q.put(my_std_command2)

    e1 = q.get_nowait()
    assert e1 == my_std_command1

    my_stop = commands.StopBlock()
    q.put(my_stop)

    
    eStop = q.get_nowait()

    print(eStop)
    assert eStop == my_stop

    assert q.get_nowait() == my_std_command2 

    
    
    
