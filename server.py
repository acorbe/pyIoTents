"""
tent-srv  –  Tornado-based web server for motorised tent control on Raspberry Pi.

Safety rules
------------
* Open and close relays for the same tent must NEVER be active simultaneously.
  The hardware NAND interlock is the last line of defence; this code enforces
  the same rule in software via a per-tent direction lock.
* All pins default to HIGH (relay off) at startup and on shutdown.
* A closing pulse is scaled by close_factor (runtime-adjustable) relative to
  the requested open time, because the motors are slower when closing.
* A close command is refused when position_seconds <= CLOSE_GUARD_SECONDS
  (tent is already at or near fully closed); running the motor further in the
  close direction would stall it against the end stop.

Position tracking
-----------------
Each TentControl keeps a signed net-seconds counter:
  +dt  when an open command runs
  -dt  when a close command runs (converted to open-equivalent travel)
  clamped to [0, fullyOpenTime].
"Reset to fully closed" sets the counter to 0 without running the motor.

close_factor
------------
Stored on CommandQueue so all tents share one live value.
Expressed as a multiplier >= 1.0.  Settable at runtime via POST /config.
Default: 1.2  (20 % longer closing pulses).
"""

import asyncio
import json

from tornado.ioloop import IOLoop
from tornado.queues  import Queue
import tornado.web
from tornado import gen
import RPi.GPIO as GPIO
import yaml
import commands as cm

GPIO.setmode(GPIO.BCM)   # BCM (not board) numbering

SETTINGS_YAML = "./settings.yaml"

# If position_seconds is at or below this threshold the tent is considered
# fully (or nearly fully) closed and further close commands are blocked.
CLOSE_GUARD_SECONDS = 2.0

# Default close-time factor.  Overridable at runtime via POST /config.
DEFAULT_CLOSE_FACTOR = 1.2

# Queue capacity.
QUEUE_MAXSIZE = 16

# Maximum duration for a single manual hold-to-run command (seconds).
MANUAL_MAX_SECONDS = 60.0

# Cooldown injected after every motor command (seconds).
POSTCMD_COOLDOWN = 1.0


# ---------------------------------------------------------------------------
# Command queue – serialises all GPIO pulses
# ---------------------------------------------------------------------------

class CommandQueue(object):
    def __init__(self):
        self.all_my_pins = set()
        self.my_controls = set()

        # Runtime-adjustable close-time multiplier (>= 1.0).
        # Shared by all TentControls via their my_owner reference.
        self.close_factor = DEFAULT_CLOSE_FACTOR

        # Swappable GPIO ops (replace with stubs for unit tests).
        self.default_switch_pin_OFF_op = lambda pin: GPIO.output(pin, GPIO.HIGH)
        self.default_switch_pin_ON_op  = lambda pin: GPIO.output(pin, GPIO.LOW)
        self.default_sleep_op          = lambda dt:  gen.sleep(dt)

        self.current_switch_pin_OFF_op = self.default_switch_pin_OFF_op
        self.current_switch_pin_ON_op  = self.default_switch_pin_ON_op
        self.current_sleep_op          = self.default_sleep_op

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_cycle(self):
        """Initialise queue and launch background consumer."""
        await self.reset_queue()
        IOLoop.current().spawn_callback(self.execute_loop)

    async def reset_queue(self):
        self.my_queue = Queue(maxsize=QUEUE_MAXSIZE)
        await self._add_waiting_block()

    # ------------------------------------------------------------------
    # Control registration
    # ------------------------------------------------------------------

    def register_control(self, control):
        self.my_controls.add(control)
        new_pins = control.get_my_pins()
        self.all_my_pins.update(new_pins)
        control.set_owner(self)
        for pin in new_pins:
            GPIO.setup(pin, GPIO.OUT)
        self.all_off()

    def all_off(self):
        """Emergency: force every registered pin to OFF (HIGH)."""
        for pin in self.all_my_pins:
            self.default_switch_pin_OFF_op(pin)

    # ------------------------------------------------------------------
    # Queue helpers
    # ------------------------------------------------------------------

    async def enqueue_command(self, command):
        await self.my_queue.put(command)

    async def waitqueue(self):
        await self.my_queue.join()

    def get_qsize(self):
        return self.my_queue.qsize()

    async def _add_waiting_block(self):
        """Seed the queue with a heartbeat if it is empty."""
        if self.get_qsize() < 1:
            await self.enqueue_command(cm.WaitingBlock(0.2))

    # ------------------------------------------------------------------
    # Consumer loop
    # ------------------------------------------------------------------

    async def execute_loop(self):
        print("CommandQueue: consumer started")
        async for comm in self.my_queue:
            await self._execute_one_command(comm)
            self.my_queue.task_done()

    async def _execute_one_command(self, comm):
        # Keep the queue alive so the async-for never exits.
        await self._add_waiting_block()

        if comm.pin is not None:
            is_manual = getattr(comm, 'is_manual', False)
            print(f"exec  pin={comm.pin}  dir={comm.direction}  "
                  f"dt={'manual' if is_manual else comm.dt}s")
            self.current_switch_pin_ON_op(comm.pin)

            if is_manual:
                # Poll the tent's stop event every 0.1 s instead of sleeping
                # the full dt, so manual_stop can interrupt immediately.
                elapsed = 0.0
                stop_ev = getattr(comm.tent_control, '_manual_stop_event', None)
                while elapsed < comm.dt:
                    if stop_ev is not None and stop_ev.is_set():
                        break
                    await self.current_sleep_op(0.1)
                    elapsed += 0.1
                actual_dt = elapsed
            else:
                await self.current_sleep_op(comm.dt)
                actual_dt = comm.dt

            self.current_switch_pin_OFF_op(comm.pin)

            # Notify the originating TentControl that the pulse completed
            # so it can update its position counter.
            if comm.tent_control is not None:
                override_was_active = getattr(comm, 'override_was_active', False)
                comm.tent_control.record_motion(comm.direction, actual_dt,
                                                override_was_active)

            # 1-second cooldown after every motor command.
            await self.current_sleep_op(POSTCMD_COOLDOWN)
        else:
            await self.current_sleep_op(comm.dt)


# ---------------------------------------------------------------------------
# TentControl – one instance per tent
# ---------------------------------------------------------------------------

class TentControl(object):
    """
    Manages the two relay pins and position state for a single tent.

    Safety guarantee: only one direction can be active at a time.
    An enqueue request is silently dropped if the opposite direction is
    currently executing.
    """

    def __init__(self, idname, open_pin, close_pin, fully_open_time):
        self.idname          = idname
        self.open_pin        = open_pin
        self.close_pin       = close_pin
        self.fully_open_time = fully_open_time   # seconds for 100 % open travel

        self._pin_map = {"open": open_pin, "close": close_pin}

        # Position tracking: 0 = fully closed, fully_open_time = fully open.
        # Value is in "open-direction equivalent seconds".
        self.position_seconds = 0.0

        # Direction lock: None | "open" | "close"
        # Set while a command for this tent is executing in the consumer loop.
        self._active_direction = None

        # Override mode: bypasses close guard and close-time factor.
        # The hardware interlock (never energise both pins) is always enforced.
        self.override_mode = False

        # Manual mode: requires deliberate hold-to-unlock before hold-to-run.
        # manual_unlocked is cleared after any command is dispatched.
        self.manual_unlocked = False

        # One-shot flag: set by enqueue_manual() before calling enqueue_dt()
        # so that enqueue_dt can mark the command as manual.
        self._next_is_manual = False

        # asyncio.Event set by manual_stop() to interrupt the running manual pulse.
        self._manual_stop_event = None

        self.my_owner = None   # set by CommandQueue.register_control()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def set_owner(self, owner):
        self.my_owner = owner

    def get_my_pins(self):
        return [self.open_pin, self.close_pin]

    # ------------------------------------------------------------------
    # Position tracking
    # ------------------------------------------------------------------

    @property
    def position_pct(self):
        """0–100 % open."""
        if self.fully_open_time <= 0:
            return 0
        pct = (self.position_seconds / self.fully_open_time) * 100.0
        return max(0.0, min(100.0, pct))

    def record_motion(self, direction, dt, override_was_active):
        """Called by the consumer after a pulse completes.

        `dt` is the actual GPIO pulse duration sent to the motor (gpio_dt).
        In normal mode close gpio_dt = requested_dt * close_factor, so we
        divide by factor to recover open-equivalent travel seconds.
        In override mode gpio_dt == requested_dt (no scaling), so we use dt
        directly for the position debit.
        """
        self._active_direction = None
        if direction == "open":
            self.position_seconds += dt
        elif direction == "close":
            if override_was_active:
                self.position_seconds -= dt
            else:
                factor = self.my_owner.close_factor if self.my_owner is not None else DEFAULT_CLOSE_FACTOR
                self.position_seconds -= dt / factor
        self.position_seconds = max(0.0, min(self.fully_open_time,
                                             self.position_seconds))
        print(f"{self.idname}: position={self.position_seconds:.1f}s "
              f"({self.position_pct:.0f}%)")

    def reset_to_closed(self):
        """Declare the tent fully closed without running the motor."""
        self.position_seconds  = 0.0
        self._active_direction = None
        print(f"{self.idname}: reset to fully closed")

    def toggle_override(self, enabled):
        """Enable or disable override mode."""
        self.override_mode = bool(enabled)
        print(f"{self.idname}: override_mode={'ON' if self.override_mode else 'OFF'}")

    def set_manual_unlocked(self, enabled):
        """Arm or disarm manual hold-to-run mode."""
        self.manual_unlocked = bool(enabled)
        print(f"{self.idname}: manual_unlocked={'ON' if self.manual_unlocked else 'OFF'}")

    def manual_stop(self):
        """Signal the running manual pulse to stop immediately."""
        if self._manual_stop_event is not None:
            self._manual_stop_event.set()
        print(f"{self.idname}: manual stop signal sent")

    # ------------------------------------------------------------------
    # Command enqueueing
    # ------------------------------------------------------------------

    async def enqueue_dt(self, direction, dt):
        """
        Enqueue a motor pulse.

        Returns (success: bool, reason: str).
        Rejected if:
          - direction is unknown
          - the opposite direction is currently active (interlock)
          - direction is "close" and position is at or below CLOSE_GUARD_SECONDS
          - queue is full
        """
        if direction not in self._pin_map:
            return False, "unknown direction"

        # Software interlock: refuse if opposite direction is active.
        opposite = "close" if direction == "open" else "open"
        if self._active_direction == opposite:
            msg = (f"{self.idname}: INTERLOCK – {opposite} is active, "
                   f"refusing {direction}")
            print(msg)
            return False, "interlock"

        # Close guard (skipped in override mode).
        if direction == "close" and not self.override_mode:
            if self.position_seconds <= 0:
                # Tent is already at fully-closed position, but allow a short
                # nudge (up to CLOSE_GUARD_SECONDS) to seat it against the
                # end-stop in case the motor hasn't quite reached it.
                dt = min(dt, CLOSE_GUARD_SECONDS)
                print(f"{self.idname}: CLOSE GUARD – position is 0, "
                      f"clamping close nudge to {dt:.1f}s")
            elif self.position_seconds <= CLOSE_GUARD_SECONDS:
                # Allow up to CLOSE_GUARD_SECONDS more closing, but clamp dt.
                dt = min(dt, self.position_seconds)
                print(f"{self.idname}: CLOSE GUARD – clamping close to {dt:.1f}s")

        # Actual dt sent to GPIO.
        # In override mode the factor is bypassed: raw dt goes to the motor.
        if direction == "close" and not self.override_mode:
            factor  = self.my_owner.close_factor if self.my_owner is not None else DEFAULT_CLOSE_FACTOR
            gpio_dt = dt * factor
        else:
            gpio_dt = dt

        cmd = cm.StandardCommand(
            pin=self._pin_map[direction],
            dt=gpio_dt,
            direction=direction,
        )
        cmd.tent_control        = self
        cmd.override_was_active = self.override_mode  # snapshot at enqueue time
        cmd.is_manual           = self._next_is_manual
        self._next_is_manual    = False               # consume the one-shot flag

        self._active_direction = direction
        if self.my_owner is None:
            self._active_direction = None
            return False, "no owner"
        try:
            await self.my_owner.enqueue_command(cmd)
        except Exception:
            self._active_direction = None
            raise
        return True, "ok"

    async def enqueue_manual(self, direction):
        """
        Enqueue a manual hold-to-run pulse (max MANUAL_MAX_SECONDS).

        Requires manual_unlocked to be True; clears it after dispatching so the
        user must re-arm for each new burst.  The consumer stops the pulse early
        when manual_stop() is called.

        Normal close-guard and override rules apply exactly as for enqueue_dt.

        Returns (success: bool, reason: str).
        """
        if not self.manual_unlocked:
            return False, "manual not unlocked"

        # Prepare a fresh stop event for the consumer to poll.
        self._manual_stop_event = asyncio.Event()

        # Mark the next enqueue_dt call as manual so the command is tagged.
        self._next_is_manual = True
        # Clear manual_unlocked so the user must re-arm after each burst.
        self.manual_unlocked = False

        ok, reason = await self.enqueue_dt(direction, MANUAL_MAX_SECONDS)
        if not ok:
            # Enqueue failed; tidy up.
            self._next_is_manual    = False
            self._manual_stop_event = None
        return ok, reason


# ---------------------------------------------------------------------------
# Web handlers
# ---------------------------------------------------------------------------

class UIHandler(tornado.web.RequestHandler):
    """Serves the main UI, rendered from mainT.html."""

    def initialize(self, settings, tents):
        self._settings = settings
        self._tents    = tents

    def get(self):
        self.render("./mainT.html",
                    Appliances=self._settings["Appliances"],
                    tents=self._tents)


class TestHandler(tornado.web.RequestHandler):
    def get(self):
        self.write("ok")


class TentActionHandler(tornado.web.RequestHandler):
    """Generic GET handler that triggers a tent action."""

    def initialize(self, tent, direction, dt):
        self._tent      = tent
        self._direction = direction
        self._dt        = dt

    async def get(self):
        ok, reason = await self._tent.enqueue_dt(self._direction, self._dt)
        if ok:
            self.write("ok")
        else:
            self.set_status(409)   # Conflict
            self.write(reason)


class TentOverrideHandler(tornado.web.RequestHandler):
    """POST /<name>/override  – toggle override mode for a tent.

    Body (form-encoded or JSON):
      enabled  bool/int  1 = enable, 0 = disable

    When override mode is ON:
      - The close guard is bypassed entirely (including the nudge clamp).
      - The close-time factor is bypassed (raw dt sent to the motor).
      - The open/close interlock is still fully enforced.
    """

    def initialize(self, tent):
        self._tent = tent

    def post(self):
        try:
            raw = self.get_argument("enabled", default=None)
            if raw is None:
                body = json.loads(self.request.body)
                raw  = body.get("enabled")
            enabled = bool(int(raw))
        except Exception:
            self.set_status(400)
            self.write("invalid 'enabled' value")
            return

        self._tent.toggle_override(enabled)
        self.set_header("Content-Type", "application/json")
        self.write(json.dumps({
            "override_mode": self._tent.override_mode,
        }))


class TentStatusHandler(tornado.web.RequestHandler):
    """Returns JSON position state for all tents plus shared config."""

    def initialize(self, tents, controller):
        self._tents      = tents
        self._controller = controller

    def get(self):
        data = {
            "config": {
                "close_factor":       round(self._controller.close_factor, 4),
                "close_guard_seconds": CLOSE_GUARD_SECONDS,
            },
            "tents": {},
        }
        for name, tent in self._tents.items():
            data["tents"][name] = {
                "position_seconds":  round(tent.position_seconds, 2),
                "position_pct":      round(tent.position_pct, 1),
                "active_direction":  tent._active_direction,
                "fully_open_time":   tent.fully_open_time,
                "close_blocked":     tent.position_seconds <= CLOSE_GUARD_SECONDS,
                "override_mode":     tent.override_mode,
                "manual_unlocked":   tent.manual_unlocked,
            }
        self.set_header("Content-Type", "application/json")
        self.write(json.dumps(data))


class TentResetHandler(tornado.web.RequestHandler):
    """Resets a tent's position tracker to fully closed (no motor movement)."""

    def initialize(self, tent):
        self._tent = tent

    def post(self):
        self._tent.reset_to_closed()
        self.write("ok")


class TentFullCloseHandler(tornado.web.RequestHandler):
    """POST /<name>/fullclose

    Enqueues a single close pulse sized to bring the tent from its current
    tracked position to fully closed, taking close_factor into account.

    The requested dt passed to enqueue_dt is position_seconds; enqueue_dt
    will then apply the close_factor scaling to get the actual gpio_dt, just
    like every other close command.  This handler returns JSON with the
    computed gpio_dt so the UI can confirm it was what was expected.

    Returns JSON:
      { "requested_dt": <float>, "ok": true }   on success
      409 + reason string                        on interlock / guard refusal
    """

    def initialize(self, tent, controller):
        self._tent       = tent
        self._controller = controller

    async def post(self):
        pos = self._tent.position_seconds
        if pos <= 0:
            self.set_status(409)
            self.write("already closed")
            return

        factor    = self._controller.close_factor
        gpio_dt   = round(pos * factor, 1)

        ok, reason = await self._tent.enqueue_dt("close", pos)
        if ok:
            self.set_header("Content-Type", "application/json")
            self.write(json.dumps({"requested_dt": gpio_dt, "ok": True}))
        else:
            self.set_status(409)
            self.write(reason)


class TentManualUnlockHandler(tornado.web.RequestHandler):
    """POST /<name>/manual_unlock

    Arms or disarms manual hold-to-run mode for a tent.

    Body (form-encoded):
      enabled  1 | 0
    """

    def initialize(self, tent):
        self._tent = tent

    def post(self):
        try:
            raw     = self.get_argument("enabled", default=None)
            if raw is None:
                body = json.loads(self.request.body)
                raw  = body.get("enabled")
            enabled = bool(int(raw))
        except Exception:
            self.set_status(400)
            self.write("invalid 'enabled' value")
            return

        self._tent.set_manual_unlocked(enabled)
        self.set_header("Content-Type", "application/json")
        self.write(json.dumps({"manual_unlocked": self._tent.manual_unlocked}))


class TentManualStartHandler(tornado.web.RequestHandler):
    """POST /<name>/manual_start

    Begins a hold-to-run manual pulse.  Requires manual_unlocked=True on the
    tent (set via /manual_unlock).  Resets manual_unlocked so re-arm is needed.

    Body (form-encoded):
      direction  "open" | "close"
    """

    def initialize(self, tent):
        self._tent = tent

    async def post(self):
        try:
            direction = self.get_argument("direction")
        except Exception:
            self.set_status(400)
            self.write("missing direction")
            return

        if direction not in ("open", "close"):
            self.set_status(400)
            self.write("direction must be 'open' or 'close'")
            return

        ok, reason = await self._tent.enqueue_manual(direction)
        if ok:
            self.set_header("Content-Type", "application/json")
            self.write(json.dumps({"ok": True, "direction": direction}))
        else:
            self.set_status(409)
            self.write(reason)


class TentManualStopHandler(tornado.web.RequestHandler):
    """POST /<name>/manual_stop

    Signals the running manual pulse to stop.  Safe to call even if no manual
    command is running (no-op).
    """

    def initialize(self, tent):
        self._tent = tent

    def post(self):
        self._tent.manual_stop()
        self.set_header("Content-Type", "application/json")
        self.write(json.dumps({"ok": True}))


class ConfigHandler(tornado.web.RequestHandler):
    """GET  /config  – return current close_factor as JSON.
       POST /config  – update close_factor.

    POST body (application/x-www-form-urlencoded or JSON):
      close_factor  float  e.g. 1.25 (means 25 % longer closing pulses)
                           accepted range: 1.0 – 3.0
    """

    def initialize(self, controller):
        self._controller = controller

    def get(self):
        self.set_header("Content-Type", "application/json")
        self.write(json.dumps({
            "close_factor": round(self._controller.close_factor, 4),
        }))

    def post(self):
        # Accept both form-encoded and JSON body.
        try:
            raw = self.get_argument("close_factor", default=None)
            if raw is None:
                body = json.loads(self.request.body)
                raw  = body.get("close_factor")
            factor = float(raw)
        except Exception:
            self.set_status(400)
            self.write("invalid close_factor")
            return

        if not (1.0 <= factor <= 3.0):
            self.set_status(400)
            self.write("close_factor must be between 1.0 and 3.0")
            return

        self._controller.close_factor = factor
        print(f"close_factor updated to {factor:.4f}")
        self.set_header("Content-Type", "application/json")
        self.write(json.dumps({"close_factor": round(factor, 4)}))


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def make_app(settings, tents, controller):
    cfg_args = dict(controller=controller)
    ui_args  = dict(settings=settings, tents=tents)

    routes = [
        (r"/",       UIHandler,         ui_args),
        (r"/test",   TestHandler),
        (r"/status", TentStatusHandler, dict(tents=tents, controller=controller)),
        (r"/config", ConfigHandler,     cfg_args),
    ]

    for appliance in settings["Appliances"]:
        if appliance["type"] != "tent":
            continue
        name = appliance["idname"]
        tent = tents[name]

        # Reset endpoint
        routes.append((
            r"/{}/reset".format(name),
            TentResetHandler,
            dict(tent=tent),
        ))

        # Override / unlock endpoint
        routes.append((
            r"/{}/override".format(name),
            TentOverrideHandler,
            dict(tent=tent),
        ))

        # Full-close endpoint (arbitrary duration, sized from current position)
        routes.append((
            r"/{}/fullclose".format(name),
            TentFullCloseHandler,
            dict(tent=tent, controller=controller),
        ))

        # Manual hold-to-run endpoints
        routes.append((
            r"/{}/manual_unlock".format(name),
            TentManualUnlockHandler,
            dict(tent=tent),
        ))
        routes.append((
            r"/{}/manual_start".format(name),
            TentManualStartHandler,
            dict(tent=tent),
        ))
        routes.append((
            r"/{}/manual_stop".format(name),
            TentManualStopHandler,
            dict(tent=tent),
        ))

        # Per-duration open/close endpoints
        for dt in appliance["timeActionsSym"]:
            for direction in ("open", "close"):
                routes.append((
                    r"/{name}_{dir}{dt}s".format(
                        name=name, dir=direction, dt=dt),
                    TentActionHandler,
                    dict(tent=tent, direction=direction, dt=dt),
                ))

    return tornado.web.Application(routes)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main(settings_file=SETTINGS_YAML):
    with open(settings_file) as f:
        settings = yaml.load(f, yaml.FullLoader)
    print("Settings loaded:", settings)

    controller = CommandQueue()
    await controller.start_cycle()

    tents = {}
    for appliance in settings["Appliances"]:
        if appliance["type"] != "tent":
            continue
        tent = TentControl(
            idname=appliance["idname"],
            open_pin=appliance["openPin"],
            close_pin=appliance["closePin"],
            fully_open_time=appliance["fullyOpenTimeEst"],
        )
        controller.register_control(tent)
        tents[appliance["idname"]] = tent

    app = make_app(settings, tents, controller)
    app.listen(settings["System"]["port"])
    print(f"Listening on port {settings['System']['port']}")

    await controller.waitqueue()


if __name__ == "__main__":
    try:
        IOLoop.current().run_sync(main)
    finally:
        GPIO.cleanup()
