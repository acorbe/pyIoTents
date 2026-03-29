"""Unit tests for commands.py.

These tests exercise the command classes and priority ordering.
They do NOT require a Raspberry Pi or a running Tornado IOLoop.
"""
import commands
from tornado.queues import PriorityQueue


def test_prqueue_insert():
    """Same-priority commands come out in insertion order."""
    q = PriorityQueue()

    cmd1 = commands.StandardCommand(pin=5, dt=0.2, direction="open")
    q.put(cmd1)

    cmd2 = commands.StandardCommand(pin=6, dt=0.3, direction="close")
    q.put(cmd2)

    e1 = q.get_nowait()
    assert e1 is cmd1

    print(e1)
    print(q.get_nowait())


def test_prqueue_insertStop():
    """StopBlock (priority 5) is dequeued before StandardCommand (priority 10)."""
    q = PriorityQueue()

    cmd1 = commands.StandardCommand(pin=5, dt=0.2, direction="open")
    q.put(cmd1)

    cmd2 = commands.StandardCommand(pin=6, dt=0.3, direction="close")
    q.put(cmd2)

    e1 = q.get_nowait()
    assert e1 is cmd1

    stop = commands.StopBlock(uuid_str="test-uuid-001")
    q.put(stop)

    e_stop = q.get_nowait()
    print(e_stop)
    assert e_stop is stop

    assert q.get_nowait() is cmd2
