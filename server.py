from tornado.ioloop import IOLoop
from tornado.queues import Queue
import tornado.web
from tornado import gen
import tornado.web
import RPi.GPIO as GPIO
import yaml
import commands as cm

GPIO.setmode(GPIO.BCM)  # GPIO Numbers instead of board numbers

SETTINGS_YAML = "./settings.yaml"

# pin target 23, 24


class CommandQueue(object):
    def __init__(self):
        
        self.all_my_pins = set()
        self.my_controls = set()

        self.default_switch_pin_OFF_op = lambda pin: GPIO.output(pin, GPIO.HIGH)
        self.default_switch_pin_ON_op = lambda pin: GPIO.output(pin, GPIO.LOW)
        self.default_sleep_op = lambda dt: gen.sleep(dt)

        self.current_switch_pin_OFF_op = self.default_switch_pin_OFF_op
        self.current_switch_pin_ON_op = self.default_switch_pin_ON_op
        self.current_sleep_op = self.default_sleep_op

        
    async def start_cycle(self):
        """starts ioloop cycle. Wrapper.
        """

        await self.reset_queue()
        IOLoop.current().spawn_callback( lambda : self.execute_loop())


    async def reset_queue(self):
        self.my_queue = Queue(maxsize = 4)

        # the waiting block is to keep the queue alive
        await self.add_waiting_block()

        
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
        """emergency mode: all pins go to zero"""
        # self.reset_queue()

        for pin in self.all_my_pins:
            # Do not change, default command should not be highjacked
            self.default_switch_pin_OFF_op(pin)
            # GPIO.output(pin, GPIO.HIGH)

    def print_qsize(self):
        print("qsize", self.get_qsize())

    def get_qsize(self):
        return self.my_queue.qsize()
    
    async def add_waiting_block(self, verbose = False):
        """Adds a waiting block ONLY if the queue needs it.
        """
        
        qsize = self.get_qsize()
        if verbose: 
            print(f"deciding on adding a waiting block (qsize: {qsize})")
            
        # A waiting block is added only if the queue is empty
        if qsize < 1:             
            await self.enqueue_command(cm.WaitingBlock(0.2))
            if verbose:
                print("adding wait block")
                self.print_qsize()
        else:
            if verbose:
                print("no waiting block!")

    async def execute_one_command(self,comm, verbose = 1):
        pin = comm.pin
        dt = comm.dt
        
        # waiting blocks keep the thermodynamic equilibrium.
        # When we execute a command, the queue gets shorter.
        # This compensates.
        # Note if the queue is long enough, the waiting block
        # won't be added.
        await self.add_waiting_block()
        
        # none for waiting blocks
        if pin is not None:
            print("processing -- pin:", pin, "t:", dt, "s")
            
            # GPIO.output(pin, GPIO.LOW) # on
            # await gen.sleep(dt)
            # GPIO.output(pin, GPIO.HIGH) # out
            self.current_switch_pin_ON_op(pin)
            await self.current_sleep_op(dt)
            self.current_switch_pin_OFF_op(pin)
            
        else:
            if verbose >= 2:
                print("processing -- waiting block",dt,"s")
            await self.current_sleep_op(dt)

    async def execute_loop(self):
        print("starting loop")

        #consumer
        #try:
        while(True):
            async for comm in self.my_queue:
                #print(self.my_queue)
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
        #  command must be in the my_pins dictionary.
        #  acts as a factory for the command
        if command in self.my_pins:
            await self.my_owner.enqueue_command(
                cm.StandardCommad(
                {'pin' : self.my_pins[command]
                 , 'dt' : dt}) )
        print("DONE:", "enqueuing", command, "for", dt)


with open("main.html") as f:
    main_page = f.read()

class MainHandler(tornado.web.RequestHandler):
    def get(self):
        self.write(main_page)

class TemplatedMainHandler(tornado.web.RequestHandler):
    def initialize(self, settings):
        self.settings = settings        
    
    def get(self):
        self.render("./mainT.html",**self.settings['Appliances'])

class TestHandler(tornado.web.RequestHandler):
    def get(self):
        self.write("test")

class MinimalTentGetter(tornado.web.RequestHandler):
    def initialize(self, foo_to_wrap,**kw):
        
        print("initialized MinimalTentGetter")
        self.foo_to_wrap = foo_to_wrap
        self.kw = kw
            
    async def get(self):
        print("called MTG")
        await self.foo_to_wrap(**self.kw)
        return "ok"

il = lambda x,**kw : dict(foo_to_wrap  = x,**kw)
def make_app(tornadoApplication_EndpointsExt, settings):
    
    app_list = [
        (r"/", MainHandler),
        (r"/test", TestHandler),
        (r"/mainT", TemplatedMainHandler, settings)
    ]

    app_list.extend(tornadoApplication_EndpointsExt)    

    return tornado.web.Application(app_list)


async def main(settings_file = SETTINGS_YAML):
    with open(settings_file) as f:
        settings = yaml.load(f, yaml.FullLoader)
        
    print(settings)

    # controller initialization and start
    my_controller = CommandQueue()
    await my_controller.start_cycle()

    tornadoApplication_EndpointsExt = []

    for appliance in settings["Appliances"]:
        if appliance["type"] == "tent":
            my_tent = TentControl(appliance["openPin"], appliance["closePin"])
            my_controller.register_control(my_tent)

            # adds basic actions endpoints for open and close for given times
            # specified in the settings 
            for dt in appliance["timeActionsSym"]:
                tornadoApplication_EndpointsExt.extend([
                    (r"/open{dt}s_{name}".format(dt=dt, name = appliance["idname"])
                     , MinimalTentGetter
                     , il(my_tent.enqueue_dt,command="open",dt=dt))
                    , (r"/close{dt}s_{name}".format(dt=dt, name = appliance["idname"]), MinimalTentGetter
                       , il(my_tent.enqueue_dt,command="close",dt=dt))
                ])


    print("loop starting past")        

    # passes the endpoints from the appliances
    app = make_app(tornadoApplication_EndpointsExt,settings)
    
    app.listen(settings["System"]["port"])

    await my_controller.waitqueue()





if __name__ == '__main__':
    # IOLoop.current().run_sync(main)
    # GPIO.cleanup()
    btn_input = 5
    ## https://raspi.tv/2014/rpi-gpio-update-and-detecting-both-rising-and-falling-edges
    # GPIO.setup(btn_input, GPIO.IN)
    # GPIO.add_event_detect(btn_input, GPIO.FALLING
    #                       , bouncetime=200, callback=lambda x : print("threaded event",x)) 

    
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



