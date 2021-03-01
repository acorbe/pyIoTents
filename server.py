#import tornado.ioloop
from tornado.ioloop import IOLoop
from tornado.queues import Queue
import tornado.web
from tornado import gen
import tornado.web
import RPi.GPIO as GPIO
GPIO.setmode(GPIO.BCM) # GPIO Numbers instead of board numbers



# pin target 23, 24

class CommandQueue(object):
    def __init__(self):
        
        self.all_my_pins = set()
        self.my_controls = set()

    def reset_queue(self):
        self.my_queue = Queue(maxsize = 4)
        
        
    async def enqueue_command(self, command):
        await self.my_queue.put(command)

    async def waitqueue(self):
        await self.my_queue.join()
        

    def register_control(self, control):
        self.my_controls.add(control)
        new_pins = control.get_my_pins()
        self.all_my_pins.update(new_pins)

        control.set_owner(self)

        for pin in new_pins:
            GPIO.setup(pin, GPIO.OUT) # GPIO Assign mode

        self.all_off()        
        

    def all_off(self):
        # emergency mode: all pins go to zero
        self.reset_queue()

        for pin in self.all_my_pins:
            GPIO.output(pin, GPIO.HIGH)

    def print_qsize(self):
        print("qsize", self.get_qsize())

    def get_qsize(self):
        return self.my_queue.qsize()
    
    async def add_waiting_block(self, verbose = False):
        
        
        qsize = self.get_qsize()
        if verbose: 
            print(f"deciding on adding a waiting block (qsize: {qsize})")
        if qsize == 1:             
            await self.enqueue_command({'pin' : None, 'dt' : 0.2})
            if verbose:
                print("adding wait block")
                self.print_qsize()
        else:
            if verbose:
                print("no waiting block!")

    async def execute_one_command(self,comm, verbose = 1):
        pin = comm['pin']
        dt = comm['dt']
        if pin is not None:
            print("processing -- pin:", pin, "t:", dt, "s")
            
            GPIO.output(pin, GPIO.LOW) # on
            await gen.sleep(dt)
            # GPIO.output(RELAIS_1_GPIO, GPIO.HIGH) # on
            GPIO.output(pin, GPIO.HIGH) # out

        else:
            if verbose >= 2:
                print("processing -- waiting block",dt,"s")
            await gen.sleep(dt)

    async def execute_loop(self):
        print("starting loop")

        #consumer
        #try:
        while(True):
            async for comm in self.my_queue:
                await self.add_waiting_block()
                await self.execute_one_command(comm)
                self.my_queue.task_done()
                

            if self.get_qsize() == 0:
                break

            

            

class TentControl(object):
    def __init__(self, open_tent_pin, close_tent_pin):
        self.open_tent_pin = open_tent_pin
        self.close_tent_pin = close_tent_pin

        self.my_pins = {'open': self.open_tent_pin
                        , 'close': self.close_tent_pin}

    def set_owner(self, owner):
        self.my_owner = owner

    def get_my_pins(self):
        return [self.open_tent_pin
                , self.close_tent_pin]

    async def enqueue_dt(self,command,dt):
        print("enqueuing", command, "for", dt)
        if command in self.my_pins:
            await self.my_owner.enqueue_command(
                {'pin' : self.my_pins[command]
                 , 'dt' : dt})
        print("DONE:", "enqueuing", command, "for", dt)

    async def enqueue_open_1s(self):
        await self.enqueue_dt("open",1)
        # print("equeuing open 1s")
        # await self.my_owner.enqueue_command(
        #     {'pin' : self.open_tent_pin
        #      , 'dt' : 1})
        # print("DONE: equeuing open 1s")

    async def enqueue_close_1s(self):
        await self.enqueue_dt("close",1)
        # print("equeuing open 1s")
        # await self.my_owner.enqueue_command(
        #     {'pin' : self.close_tent_pin
        #      , 'dt' : 1})
        # print("DONE: equeuing close 1s")


    async def enqueue_open_3s(self):
        await self.enqueue_dt("open",3)
        # print("equeuing open 3s")
        # await self.my_owner.enqueue_command(
        #     {'pin' : self.open_tent_pin
        #      , 'dt' : 3})
        # print("DONE: equeuing open 3s")

    async def enqueue_close_3s(self):
        await self.enqueue_dt("close",3)
        # print("equeuing close 3s")
        # await self.my_owner.enqueue_command(
        #     {'pin' : self.close_tent_pin
        #      , 'dt' : 3})
        # print("DONE: equeuing close 3s")
        

with open("main.html") as f:
    main_page = f.read()

class MainHandler(tornado.web.RequestHandler):
    def get(self):
        self.write(main_page)

class TestHandler(tornado.web.RequestHandler):
    def get(self):
        self.write("test")


async def main():
    my_tent = TentControl(24,23)
    my_controller = CommandQueue()

    my_controller.register_control(my_tent)

    ## A queue with two elements seems important to start
    await my_tent.enqueue_open_1s()
    await my_tent.enqueue_open_1s()

    #my_controller.execute_loop
    IOLoop.current().spawn_callback( lambda : my_controller.execute_loop())

    print("loop starting past")

    class MinimalTentGetter(tornado.web.RequestHandler):
        def initialize(self, foo_to_wrap,**kw):
            
            print("initialized MinimalTentGetter")
            self.foo_to_wrap = foo_to_wrap
            self.kw = kw
            
        async def get(self):
            print("called MTG")
            await self.foo_to_wrap(**self.kw)
            return "ok"
        

    def make_app():
        il = lambda x : dict(foo_to_wrap  = x)
        return tornado.web.Application([
            (r"/", MainHandler),
            (r"/test", TestHandler),
            (r"/open1s", MinimalTentGetter, il(my_tent.enqueue_open_1s)), #MinimalTentGetter(my_tent.enqueue_open_1s)),
            (r"/open3s", MinimalTentGetter, il(my_tent.enqueue_open_3s)),
            (r"/close1s", MinimalTentGetter, il(my_tent.enqueue_close_1s)),
            (r"/close3s", MinimalTentGetter, il(my_tent.enqueue_close_3s))
        ])

    app = make_app()
    app.listen(9000)

    await my_controller.waitqueue()





if __name__ == '__main__':
    # IOLoop.current().run_sync(main)
    # GPIO.cleanup()
    try:
        IOLoop.current().run_sync(main)
    finally:
        GPIO.cleanup()


# def run_action_for_t(pin, dt):
#     RELAIS_1_GPIO = pin
#     GPIO.setup(RELAIS_1_GPIO, GPIO.OUT) # GPIO Assign mode
#     #GPIO.output(RELAIS_1_GPIO, GPIO.LOW) # out
#     GPIO.output(RELAIS_1_GPIO, GPIO.HIGH) # on
    
#     yield gen.sleep(dt)
#     # GPIO.output(RELAIS_1_GPIO, GPIO.HIGH) # on
#     GPIO.output(RELAIS_1_GPIO, GPIO.LOW) # out



