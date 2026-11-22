# PROFESSOR Mech Royale bot, by Sparsh
"""Target tracking and aiming maths."""
import math
from collections import deque


def wrap(angle):
    return (angle + math.pi) % math.tau - math.pi


def clamp(value, low, high):
    return min(high, max(low, value))


def angles(origin, point):
    dx, dy, dz = point[0] - origin[0], point[1] - origin[1], point[2] - origin[2]
    return [math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))]


class Track:
    """One enemy's recent motion."""

    def __init__(self):
        self.samples = deque(maxlen=8)
        self.info = {}
        self.seen = -math.inf

    def update(self, info, now):
        if now - self.seen > 0.5:
            self.samples.clear()
        self.samples.append(tuple(info["vel"]))
        self.info, self.seen = info, now

    def predict(self, seconds):
        """Displacement after seconds.

        Trust the current velocity for short flights. For long ones lean on the recent
        average, because a target that is juking left and right cancels itself out.
        """
        current = self.samples[-1]
        average = [sum(axis) / len(self.samples) for axis in zip(*self.samples)]
        trust = clamp(1.15 - seconds, 0.45, 1.0)
        shift = [(c * trust + a * (1 - trust)) * seconds for c, a in zip(current, average)]
        shift[2] = current[2] * min(seconds, 0.2)  # vertical speed rarely lasts
        return shift


def lead(origin, point, track, speed, delay):
    """Where to aim a straight projectile, refining the flight time a few times."""
    flight, spot = math.dist(origin, point) / speed, point
    for _ in range(4):
        spot = [p + s for p, s in zip(point, track.predict(delay + flight))]
        flight = math.dist(origin, spot) / speed
    return spot


def lob(origin, point, track, speed, gravity):
    """Low-arc mortar solution as (yaw, pitch, flight time, landing point), or None."""
    flight, answer = 0.0, None
    for _ in range(4):
        spot = [p + s for p, s in zip(point, track.predict(flight))]
        dx, dy, dz = spot[0] - origin[0], spot[1] - origin[1], spot[2] - origin[2]
        flat = max(1.0, math.hypot(dx, dy))
        square = speed ** 4 - gravity * (gravity * flat * flat + 2 * dz * speed * speed)
        if square < 0:
            return None
        pitch = math.atan((speed * speed - math.sqrt(square)) / (gravity * flat))
        flight = flat / (speed * math.cos(pitch))
        answer = (math.atan2(dy, dx), pitch, flight, spot)
    return answer


def arc_points(origin, solution, speed, gravity, pieces=6):
    yaw, pitch, flight, _ = solution
    flat, up = speed * math.cos(pitch), speed * math.sin(pitch)
    times = [flight * k / pieces for k in range(pieces + 1)]
    return [(origin[0] + flat * math.cos(yaw) * t, origin[1] + flat * math.sin(yaw) * t,
             origin[2] + up * t - gravity * t * t / 2) for t in times]


def slew(current, wanted, stats, seconds=0.05):
    """The torso's aim one simulation tick from now, which is what the next shot follows."""
    yaw_step, pitch_step = stats["turn_rate"] * seconds, stats["pitch_rate"] * seconds
    return [wrap(current[0] + clamp(wrap(wanted[0] - current[0]), -yaw_step, yaw_step)),
            current[1] + clamp(wanted[1] - current[1], -pitch_step, pitch_step)]


def on_target(actual, wanted, distance, half_width, half_height):
    sideways = abs(wrap(actual[0] - wanted[0])) * math.cos(wanted[1]) * distance
    vertical = abs(actual[1] - wanted[1]) * distance
    return sideways <= half_width and vertical <= half_height
