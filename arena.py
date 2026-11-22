# PROFESSOR Mech Royale bot, by Sparsh
"""The static map: ground height, line of fire and walking routes."""
import array
import heapq
import json
import math
import os
import sys

CELL = 6.0  # routing grid, metres
BUCKET = 32.0  # spatial index for solids, metres
NEIGHBOURS = [(dx, dy, math.hypot(dx, dy)) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy]


class Solid:
    """A prism from world.json."""

    def __init__(self, data):
        self.low, self.high = data["base"], data["top"]
        self.edges = []
        for ring in [data["ring"]] + (data.get("holes") or []):
            points = list(zip(ring[0::2], ring[1::2]))
            self.edges += zip(points, points[1:] + points[:1])
        xs, ys = data["ring"][0::2], data["ring"][1::2]
        self.box = (min(xs), min(ys), max(xs), max(ys))

    def contains(self, x, y):
        inside = False
        for (ax, ay), (bx, by) in self.edges:
            if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
                inside = not inside
        return inside

    def within(self, x, y, margin):
        """Is the point inside the footprint, or closer to it than margin?"""
        for (ax, ay), (bx, by) in self.edges:
            ex, ey = bx - ax, by - ay
            along = ((x - ax) * ex + (y - ay) * ey) / (ex * ex + ey * ey or 1.0)
            along = min(1.0, max(0.0, along))
            px, py = ax + along * ex - x, ay + along * ey - y
            if px * px + py * py < margin * margin:
                return True
        return self.contains(x, y)

    def stops(self, a, b):
        """Does the 3-D segment a-b pass through this solid?"""
        x0, y0, x1, y1 = self.box
        if max(a[0], b[0]) < x0 or min(a[0], b[0]) > x1 or max(a[1], b[1]) < y0 or min(a[1], b[1]) > y1:
            return False
        if max(a[2], b[2]) < self.low or min(a[2], b[2]) > self.high:
            return False
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        first, last = 0.0, 1.0  # part of the segment inside the height band
        if dz:
            t0, t1 = sorted(((self.low - a[2]) / dz, (self.high - a[2]) / dz))
            first, last = max(0.0, t0), min(1.0, t1)
            if first >= last:
                return False
        if self.contains(a[0] + dx * first, a[1] + dy * first):
            return True
        for (px, py), (qx, qy) in self.edges:
            ex, ey = qx - px, qy - py
            det = dx * ey - dy * ex
            if det:
                wx, wy = px - a[0], py - a[1]
                if first <= (wx * ey - wy * ex) / det <= last and 0 <= (wx * dy - wy * dx) / det <= 1:
                    return True
        return False


class Arena:
    def __init__(self, directory, mech):
        self.ready = False
        self.gap = 0.0  # how far short of its goal the last route stopped
        try:
            self._load(directory, mech)
            self.ready = True
        except (OSError, ValueError, KeyError) as error:
            print(f"no map, playing without one: {error}", file=sys.stderr, flush=True)

    def _load(self, directory, mech):
        with open(os.path.join(directory, "v2", "world.json"), encoding="utf-8") as handle:
            world = json.load(handle)
        grid = world["terrain"]
        self.t_x, self.t_y, self.t_step = grid["minX"], grid["minY"], grid["step"]
        self.t_nx, self.t_ny, self.t_scale = grid["nx"], grid["ny"], grid["scale"]
        self.terrain = array.array("h")
        with open(os.path.join(directory, "v2", grid["file"]), "rb") as handle:
            self.terrain.frombytes(handle.read())
        if sys.byteorder == "big":
            self.terrain.byteswap()

        edge = world["bounds"]
        self.x0, self.y0, self.x1, self.y1 = edge["minX"], edge["minY"], edge["maxX"], edge["maxY"]

        self.buckets = {}
        solids = [Solid(prism) for prism in world["prisms"]]
        for solid in solids:
            x0, y0, x1, y1 = solid.box
            for i in range(int(x0 // BUCKET), int(x1 // BUCKET) + 1):
                for j in range(int(y0 // BUCKET), int(y1 // BUCKET) + 1):
                    self.buckets.setdefault((i, j), []).append(solid)

        self.cols = int((self.x1 - self.x0) / CELL)
        self.rows = int((self.y1 - self.y0) / CELL)
        self.floor = []
        self.open = []
        for j in range(self.rows):
            for i in range(self.cols):
                self.floor.append(self.ground(*self.centre((i, j))))
                self.open.append(0 < i < self.cols - 1 and 0 < j < self.rows - 1)
        self.max_rise = math.tan(math.radians(mech["max_slope_deg"])) * CELL

        # Walls can be thinner than a cell, so also test the point half-way along every step.
        self.f_cols, self.f_rows = self.cols * 2 + 1, self.rows * 2 + 1
        self.solid_at = bytearray(self.f_cols * self.f_rows)
        half, margin = CELL / 2, mech["radius"] + 0.3
        for solid in solids:
            x0, y0, x1, y1 = solid.box
            left = max(0, int((x0 - margin - self.x0) / half))
            right = min(self.f_cols - 1, int((x1 + margin - self.x0) / half) + 1)
            bottom = max(0, int((y0 - margin - self.y0) / half))
            top = min(self.f_rows - 1, int((y1 + margin - self.y0) / half) + 1)
            for j in range(bottom, top + 1):
                for i in range(left, right + 1):
                    index = j * self.f_cols + i
                    if self.solid_at[index]:
                        continue
                    x, y = self.x0 + i * half, self.y0 + j * half
                    floor = self.ground(x, y)
                    if solid.high <= floor + mech["step_up"] or solid.low >= floor + mech["height"]:
                        continue
                    if solid.within(x, y, margin):
                        self.solid_at[index] = 1
                        if i % 2 and j % 2 and i // 2 < self.cols and j // 2 < self.rows:
                            self.open[(j // 2) * self.cols + i // 2] = False

    def ground(self, x, y):
        fx = min(self.t_nx - 1.001, max(0.0, (x - self.t_x) / self.t_step))
        fy = min(self.t_ny - 1.001, max(0.0, (y - self.t_y) / self.t_step))
        i, j = int(fx), int(fy)
        u, v = fx - i, fy - j
        k, h = j * self.t_nx + i, self.terrain
        near = h[k] * (1 - u) + h[k + 1] * u
        far = h[k + self.t_nx] * (1 - u) + h[k + self.t_nx + 1] * u
        return (near * (1 - v) + far * v) * self.t_scale

    def clear(self, a, b):
        """Can a shot travel from a to b?"""
        if not self.ready:
            return True
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        steps = int(length / 6) + 1
        for k in range(1, steps):
            f = k / steps
            if a[2] + (b[2] - a[2]) * f < self.ground(a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f):
                return False
        tested = set()
        steps = int(length / (BUCKET / 4)) + 1
        for k in range(steps + 1):
            f = k / steps
            key = (int((a[0] + (b[0] - a[0]) * f) // BUCKET), int((a[1] + (b[1] - a[1]) * f) // BUCKET))
            for solid in self.buckets.get(key, ()):
                if solid not in tested:
                    tested.add(solid)
                    if solid.stops(a, b):
                        return False
        return True

    def cell(self, x, y):
        return (min(self.cols - 1, max(0, int((x - self.x0) / CELL))),
                min(self.rows - 1, max(0, int((y - self.y0) / CELL))))

    def centre(self, cell):
        return self.x0 + (cell[0] + 0.5) * CELL, self.y0 + (cell[1] + 0.5) * CELL

    def walkable(self, x, y):
        if not self.ready:
            return True
        i, j = self.cell(x, y)
        return self.open[j * self.cols + i]

    def route(self, start, end, budget=2200):
        """Weighted A*. When the budget runs out, returns the route to the closest cell found."""
        if not self.ready:
            return [tuple(end[:2])]
        source, goal = self.cell(*start[:2]), self.cell(*end[:2])
        cost, came = {source: 0.0}, {}
        queue, closed = [(0.0, source)], set()
        best, best_left = source, math.dist(source, goal)
        while queue and len(closed) < budget:
            _, here = heapq.heappop(queue)
            if here in closed:
                continue
            closed.add(here)
            left = math.dist(here, goal)
            if left < best_left:
                best, best_left = here, left
            if left < 1.0:
                break
            x, y = here
            floor = self.floor[y * self.cols + x]
            for dx, dy, length in NEIGHBOURS:
                i, j = x + dx, y + dy
                if not (0 <= i < self.cols and 0 <= j < self.rows):
                    continue
                index = j * self.cols + i
                if not self.open[index] or self.floor[index] - floor > self.max_rise * length:
                    continue
                if self.solid_at[(2 * y + 1 + dy) * self.f_cols + 2 * x + 1 + dx]:
                    continue
                total = cost[here] + length
                if total < cost.get((i, j), math.inf):
                    cost[(i, j)], came[(i, j)] = total, here
                    heapq.heappush(queue, (total + 1.4 * math.dist((i, j), goal), (i, j)))
        path = []
        self.gap = best_left * CELL
        while best != source:
            path.append(self.centre(best))
            best = came[best]
        path.reverse()
        return path
