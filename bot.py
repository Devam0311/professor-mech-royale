# PROFESSOR Mech Royale bot, by Sparsh
"""Plan: get a laser early, shoot accurately, and press every fight that can be won."""
import math

import mechfight as mf
from arena import Arena
from gunnery import Track, angles, arc_points, clamp, lead, lob, on_target, slew, wrap

DECISION = 0.1  # replaced by the configured value at start-up
DANGER_SECONDS = 5.0  # run when we would be dead this soon
RETREAT_HP, RETURN_HP = 0.25, 0.55  # health fractions for breaking off and rejoining
HEADINGS = [k * math.tau / 20 for k in range(20)]
UPGRADES = ["laser_damage", "laser_charge", "gun_damage", "gun_velocity", "gun_rate", "plating",
            "armor", "gun_mag", "gyros", "nanites", "servos", "laser_pierce", "lightning_range"]
WEAPON_RANK = {"laser": 3, "lightning": 2, "mortar": 1, "gun": 0}


def flat_distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Professor:
    name = "PROFESSOR"

    def __init__(self, hello):
        self.config = config = hello["config"]
        self.arena = Arena(hello["map"]["dir"], config["mech"])
        match = config["match"]
        self.step = match["decide_every"] / match["tick_hz"] if match else DECISION
        self.tracks = {}
        self.items = {}  # id -> (category, item, time last seen)
        self.skipped = {}  # id -> time it may be tried again
        self.attackers = {}  # id -> time it last hurt us
        self.target = None
        self.retreating = False
        self.retreat_since = -math.inf
        self.hurt = []  # (time, damage) over the last few seconds
        self.contact = -math.inf
        self.peek = None  # (target id, where we last had a shot at it, when)
        self.errand = (None, 0.0)
        self.path, self.path_goal, self.path_time = [], None, -math.inf
        self.cover, self.cover_time = None, -math.inf
        self.stuck, self.detour_until = 0, -math.inf
        self.detour_side = 1
        self.last_move = [0.0, 0.0]
        self.alert = None

    # Perception

    def observe(self, obs):
        self.t = now = obs["t"]
        self.obs, self.me = obs, obs["self"]
        self.pos, self.stats = self.me["pos"], self.me["stats"]
        self.hp = self.me["hp"] / self.me["max_hp"]
        self.muzzle = [self.pos[0], self.pos[1], self.pos[2] + self.stats["muzzle_height"]]
        self.slots = {w["kind"]: (i, w) for i, w in enumerate(self.me["weapons"]) if w}

        self.enemies = []
        for info in obs["mechs"]:
            track = self.tracks.setdefault(info["id"], Track())
            track.update(info, now)
            self.enemies.append(self.assess(info, track))

        for category, key in (("weapon", "pickups"), ("cube", "cubes"), ("crate", "crates")):
            for item in obs[key]:
                self.items[item["id"]] = (category, item, now)
        listed = {item["id"] for key in ("pickups", "cubes", "crates") for item in obs[key]}
        for key, (category, item, seen) in list(self.items.items()):
            # Forget anything that is gone when we are standing next to where it was.
            missing = key not in listed and math.dist(item["pos"], self.pos) < 12
            if missing or (category != "weapon" and now - seen > 20):
                del self.items[key]

        for event in obs["events"]:
            kind = event["type"]
            if kind == "damaged" and event.get("weapon") != "zone":
                self.hurt.append((now, event["amount"]))
            if kind == "damaged" and event.get("bearing") is not None:
                self.alert = (event["bearing"], now)
                if event.get("by") is not None:
                    self.attackers[event["by"]] = now
            elif kind == "took":
                self.items.pop(event["id"], None)
            elif kind == "take_failed":
                if event["reason"] == "gone":
                    self.items.pop(event["id"], None)
                else:
                    self.skipped[event["id"]] = now + 2
        self.hurt = [(when, amount) for when, amount in self.hurt if now - when < 3]
        if self.alert is None or now - self.alert[1] > 2:
            for sound in obs["sounds"]:
                if sound["kind"] in ("gun", "laser", "laser_charge", "lightning") and sound["dist"] != "far":
                    self.alert = (sound["bearing"], now)
                    break

    def assess(self, info, track):
        """Size up a visible target and find the best exposed part of it to shoot."""
        mech = info["kind"] == "mech"
        body = self.config["mech"] if mech else self.config["npc"]
        scale = 1 + self.config["cubes"]["size_per_cube"] * info.get("cubes", 0) if mech else 1
        height, radius = body["height"] * scale, body["radius"] * scale
        x, y, z = info["pos"]
        enemy = {"info": info, "track": track, "id": info["id"], "mech": mech, "height": height,
                 "radius": radius, "distance": math.dist(info["pos"], self.pos), "point": None}
        if enemy["distance"] < 290:
            for part in (0.55, 0.85, 0.25):  # chest, head, legs
                point = [x, y, z + height * part]
                if self.arena.clear(self.muzzle, point):
                    enemy["point"] = point
                    enemy["half_height"] = height * min(part, 1 - part)
                    break
        return enemy

    # Decisions

    def choose_target(self):
        best, best_score = None, -math.inf
        for enemy in self.enemies:
            info, distance = enemy["info"], enemy["distance"]
            if not enemy["mech"] and distance > 100:
                continue
            score = -distance
            if enemy["point"]:
                score += 120
            if enemy["mech"]:
                score += 70 + 140 * (1 - info.get("hp", 1400) / info.get("max_hp", 1400))
                if "mortar" in info.get("weapons", ()) and "laser" in self.slots:
                    score += 30  # cook-off bonus
                if self.t - self.attackers.get(enemy["id"], -math.inf) < 3:
                    score += 50
                if info.get("charging"):
                    score += 20
            if enemy["id"] == self.target:
                score += 35  # changing target wastes turn time
            if score > best_score:
                best, best_score = enemy, score
        self.target = best["id"] if best else None
        return best

    def damage_output(self):
        """Rough damage per second we deal while we have a shot."""
        total = 0.0
        for kind, (_, weapon) in self.slots.items():
            spec = weapon["stats"]
            if kind == "gun":
                total += spec["damage"] * spec["mag"] / (spec["mag"] * spec["cooldown"] + spec["reload"]) * 0.8
            elif kind == "laser":
                total += spec["damage"] / (spec["charge"] + spec["cooldown"])
        return total

    def update_retreat(self, target):
        shooters = sum(1 for e in self.enemies if e["mech"] and e["point"] and e["distance"] < 140)
        incoming = sum(amount for _, amount in self.hurt) / 3
        time_left = self.me["hp"] / incoming if incoming else math.inf
        danger = time_left < DANGER_SECONDS
        overwhelmed = shooters >= 3 and self.hp < 0.5

        # Stay aggressive when the target will clearly die before we do.
        finishing = False
        if target and target["mech"] and target["point"] and shooters < 3:
            kill_time = target["info"].get("hp", math.inf) / max(1.0, self.damage_output())
            finishing = kill_time < min(3.0, time_left * 0.6)

        if finishing:
            self.retreating = False
        elif self.hp < RETREAT_HP or overwhelmed or danger:
            if not self.retreating:
                self.retreating, self.retreat_since = True, self.t
        elif self.hp >= RETURN_HP and self.t - self.retreat_since > 2:
            self.retreating = False
        self.finishing = finishing
        self.pressured = bool(self.hurt) and (self.hp < 0.75 or shooters >= 2)

    def cover_spot(self, threats, peeking=False):
        """Find a hiding place. When peeking it has to be close and really hidden."""
        if self.t - self.cover_time < 0.5:
            return self.cover
        x, y, z = self.pos
        centre, radius = self.obs["zone"]["center"], self.obs["zone"]["radius"]
        best, best_score = None, -math.inf
        for distance in ((6, 12, 20) if peeking else (10, 22, 40)):
            for heading in HEADINGS[::2]:
                px, py = x + distance * math.cos(heading), y + distance * math.sin(heading)
                if flat_distance((px, py), centre) > radius or not self.arena.walkable(px, py):
                    continue
                score = -distance * (2.0 if peeking else 0.5)
                ground = self.arena.ground(px, py) if self.arena.ready else z
                hidden = 0
                for threat in threats[:2]:
                    tx, ty, tz = threat["info"]["pos"]
                    if not self.arena.clear([tx, ty, tz + 7], [px, py, ground + 6]):
                        score += 100
                        hidden += 1
                    elif not peeking:
                        score += 0.5 * flat_distance((px, py), (tx, ty))
                if peeking and not hidden:
                    continue
                if score > best_score:
                    best, best_score = (px, py, z), score
        self.cover, self.cover_time = best, self.t
        return best

    def ready_to_peek(self):
        """Step out just before the main weapon can fire, step back while it recovers."""
        if "laser" in self.slots:
            weapon = self.slots["laser"][1]
            return bool(weapon.get("charge")) or weapon["cooldown"] < 0.6
        gun = self.slots.get("gun")
        return not gun or (gun[1].get("reload") or 0) < 0.5

    def nearest_item(self, category, limit, kind=None, climb=3.0):
        zone = self.obs["zone"]
        best, best_distance = None, limit
        for key, (group, item, _) in self.items.items():
            if group != category or self.skipped.get(key, 0) > self.t:
                continue
            if kind and item.get("kind") != kind:
                continue
            place = item["pos"]
            if flat_distance(place, zone["center"]) > zone["radius"] - 5:
                continue
            rise = place[2] - self.pos[2]
            distance = flat_distance(place, self.pos) + 2 * max(0.0, rise)
            if self.arena.ready:
                above_ground = place[2] - self.arena.ground(place[0], place[1])
                if above_ground > 3 and rise > climb:
                    continue  # on a roof that is too high to bother with
            if distance < best_distance:
                best, best_distance = item, distance
        return best

    def give_up_if_slow(self, item):
        key, since = self.errand
        if key != item["id"]:
            self.errand = (item["id"], self.t)
        elif self.t - since > 25:
            self.skipped[key] = self.t + 30
            self.errand = (None, self.t)

    def choose_goal(self, target):
        """Returns (position, hurry)."""
        zone, pos = self.obs["zone"], self.pos
        centre, next_centre = zone["center"], zone["next_center"]
        z = pos[2]
        if not self.me["in_zone"] or flat_distance(pos, centre) > zone["radius"] - 4:
            return (next_centre[0], next_centre[1], z), True
        outside_next = flat_distance(pos, next_centre) - zone["next_radius"] * 0.85
        if outside_next > 0:
            travel = 1.5 * outside_next / self.stats["walk_speed"]
            if self.t + travel + 4 > zone["shrink_end"]:
                return (next_centre[0], next_centre[1], z), True

        threats = sorted((e for e in self.enemies if e["mech"] and e["point"]), key=lambda e: e["distance"])
        if threats:
            self.contact = self.t
        if self.retreating:
            if threats:
                spot = self.cover_spot(threats)
                if spot:
                    return spot, True
            elif self.cover and self.t - self.contact < 4:
                return self.cover, True  # they lost sight of us, so stay put and heal

        # Fighting from cover: shoot, duck back, then return to the same firing spot.
        if target and target["mech"] and not self.retreating and not self.finishing:
            if target["point"]:
                self.peek = (target["id"], tuple(pos), self.t)
                if self.pressured and not self.ready_to_peek():
                    spot = self.cover_spot(threats, peeking=True)
                    if spot:
                        return spot, True
            elif self.peek and self.peek[0] == target["id"] and self.t - self.peek[2] < 4:
                if not self.ready_to_peek():
                    return pos, False
                if flat_distance(pos, self.peek[1]) > 2:
                    return self.peek[1], True

        cube = self.nearest_item("cube", 25 if threats else 70)
        if cube:
            return cube["pos"], True

        # The laser comes before every fight that is not already on top of us.
        if "laser" not in self.slots:
            pressed = bool(threats) and threats[0]["distance"] < 30
            laser = self.nearest_item("weapon", 45 if pressed else 350, "laser", climb=35.0)
            if laser:
                self.give_up_if_slow(laser)
                return laser["pos"], True
            if len(self.slots) < 2 and not pressed:
                weapon = self.nearest_item("weapon", 50)
                if weapon:
                    self.give_up_if_slow(weapon)
                    return weapon["pos"], True

        for drop in self.obs.get("drops") or ():
            distance = flat_distance(pos, drop["pos"])
            limit = 250 if self.hp < 0.6 else 120
            if distance < limit and drop["lands_in"] < distance / self.stats["walk_speed"] + 3:
                return drop["pos"], distance > drop["radius"]

        if target and not self.retreating and (target["mech"] or target["distance"] < 70):
            return self.attack_position(target), target["distance"] > 110 or not target["point"]

        crate = self.nearest_item("crate", 120)
        if crate:
            self.give_up_if_slow(crate)
            return crate["pos"], False

        if not self.retreating:
            recent = [k for k in self.tracks.values()
                      if k.info.get("kind") == "mech" and self.t - k.seen < 10
                      and flat_distance(k.info["pos"], pos) > 15]
            if recent:
                return max(recent, key=lambda k: k.seen).info["pos"], True
        if self.t % 80 < 40 and flat_distance(pos, next_centre) > 25:
            return (next_centre[0], next_centre[1], z), False
        # Sweep the next circle for loot and targets.
        angle, radius = int(self.t / 12) * 2.4, zone["next_radius"] * 0.5
        return (next_centre[0] + radius * math.cos(angle), next_centre[1] + radius * math.sin(angle), z), False

    def attack_position(self, target):
        if not target["point"]:
            return target["info"]["pos"]  # close in until there is a shot
        if not target["mech"]:
            ideal = 35
        elif "laser" in self.slots:
            ideal = 70
        elif "lightning" in self.slots:
            ideal = 32
        else:
            ideal = 50
        tx, ty, tz = target["info"]["pos"]
        side = 1 if int(self.t / 2.3) % 2 else -1
        bearing = math.atan2(self.pos[1] - ty, self.pos[0] - tx) + side * 0.4
        return (tx + ideal * math.cos(bearing), ty + ideal * math.sin(bearing), tz)

    # Movement

    def danger_lines(self):
        """Laser sights pointing near us."""
        lines = []
        for enemy in self.enemies:
            info = enemy["info"]
            if info.get("charging") and enemy["point"]:
                yaw = info["aim"][0]
                to_us = math.atan2(self.pos[1] - info["pos"][1], self.pos[0] - info["pos"][0])
                if abs(wrap(to_us - yaw)) < 0.35:
                    lines.append((info["pos"], math.cos(yaw), math.sin(yaw)))
        return lines

    def incoming(self):
        threats = []
        x, y, z = self.pos
        gravity = self.config["weapons"]["mortar"]["gravity"]
        for shot in self.obs["projectiles"]:
            (px, py, pz), (vx, vy, vz) = shot["pos"], shot["vel"]
            if shot["kind"] == "mortar":
                root = vz * vz + 2 * gravity * (pz - z)
                if root >= 0:
                    when = (vz + math.sqrt(root)) / gravity
                    if when < 2:
                        land = (px + vx * when, py + vy * when)
                        threats.append((land, (0.0, 0.0), when, when, 9.0, 8.0))
            else:
                square = vx * vx + vy * vy
                if square:
                    when = ((x - px) * vx + (y - py) * vy) / square
                    if 0 < when < 1 and flat_distance((px + vx * when, py + vy * when), (x, y)) < 12:
                        if z - 2 < pz + vz * when < z + self.config["mech"]["height"] * 1.5:
                            threats.append(((px, py), (vx, vy), 0.0, when + 0.1, 3.5, 2.0))
        return threats

    def follow_path(self, goal):
        x, y, z = self.pos
        on_roof = self.arena.ready and z - self.arena.ground(x, y) > 4
        if on_roof or flat_distance(self.pos, goal) < 12:
            return goal
        if self.path_goal is None or self.t - self.path_time > 1.2 or (
                self.t - self.path_time > 0.4 and flat_distance(goal, self.path_goal) > 20):
            self.path = self.arena.route(self.pos, goal)
            self.path_goal, self.path_time = goal, self.t
            if self.arena.gap > 14 and len(self.path) < 4:
                # As close as walking gets: stop wasting time on loot we cannot reach.
                for key, (_, item, _) in self.items.items():
                    if item["pos"] == goal:
                        self.skipped[key] = self.t + 30
        while self.path and flat_distance(self.path[0], self.pos) < 7:
            self.path.pop(0)
        return self.path[0] if self.path else goal

    def steer(self, goal, hurry):
        x, y, z = self.pos
        vx, vy, _ = self.me["vel"]
        waypoint = self.follow_path(goal)
        arrived = flat_distance(self.pos, goal) < 2
        wanted = math.atan2(waypoint[1] - y, waypoint[0] - x)
        climbing = goal[2] > z + 2 and flat_distance(self.pos, goal) < 22

        pushing = math.hypot(*self.last_move) > 0.5
        self.stuck = self.stuck + 1 if pushing and math.hypot(vx, vy) < 1.5 else 0
        if self.stuck >= 6:
            self.stuck, self.detour_until = 0, self.t + 0.9
            self.detour_side = -self.detour_side
            self.path_goal = None
        detour = self.t < self.detour_until
        if detour:
            wanted += self.detour_side * 1.3

        lines, shots = self.danger_lines(), self.incoming()
        speed = self.stats["boost_speed" if hurry or lines else "walk_speed"]
        zone = self.obs["zone"]
        options = [(math.cos(h), math.sin(h), h) for h in HEADINGS + [wanted]] + [(0.0, 0.0, None)]
        best, best_score = options[-1], -math.inf
        for option in options:
            dx, dy, heading = option
            if heading is None:
                score = 25.0 if arrived else -5.0
            else:
                score = (3.0 if arrived else 22.0) * math.cos(heading - wanted)
                if not climbing and not detour:
                    if not self.arena.walkable(x + dx * 6, y + dy * 6):
                        score -= 14
            for start, velocity, begin, end, reach, weight in shots:
                closest = math.inf
                for k in range(5):
                    when = begin + (end - begin) * k / 4
                    closest = min(closest, math.hypot(x + dx * speed * when - start[0] - velocity[0] * when,
                                                      y + dy * speed * when - start[1] - velocity[1] * when))
                if closest < reach:
                    score -= weight * (1 + reach - closest)
            px, py = x + dx * speed * 0.6, y + dy * speed * 0.6
            for origin, lx, ly in lines:
                miss = abs((px - origin[0]) * ly - (py - origin[1]) * lx)
                score -= 5 * max(0.0, 7 - miss)
            if flat_distance((px, py), zone["center"]) > zone["radius"] - 3:
                outwards = dx * (x - zone["center"][0]) + dy * (y - zone["center"][1])
                if outwards >= 0:
                    score -= 30
            if score > best_score:
                best, best_score = option, score

        moving = best[2] is not None
        self.last_move = [best[0], best[1]] if moving else [0.0, 0.0]
        energy = self.me["energy"] / self.me["max_energy"]
        in_air = not self.me["grounded"]
        if climbing or detour:
            jet = in_air and energy > 0.03
            boost = jet or (hurry and not climbing and energy > 0.35)
            return list(self.last_move), boost, self.me["grounded"]
        boost = moving and not in_air and (bool(lines) or (hurry and energy > 0.3))
        return list(self.last_move), boost, False

    # Shooting

    def shoot(self, target):
        """Returns (aim, fire, reload)."""
        fire = [False, False]
        current = self.me["aim"]
        stats, muzzle = self.stats, self.muzzle
        gun = self.slots.get("gun")
        laser = self.slots.get("laser")
        charge = laser[1].get("charge", 0) if laser else 0
        visible = target is not None and target["point"] is not None

        if not visible:
            if charge:
                # Keep the charge for as long as possible in case the target reappears.
                slot, weapon = laser
                limit = weapon["stats"]["charge"] + weapon["stats"]["hold_s"]
                fire[slot] = charge + self.step < limit
            reload = self.want_reload(gun, target)
            mortar = self.slots.get("mortar")
            if target and target["mech"] and mortar and not charge:
                solution = self.mortar_solution(target, mortar[1])
                if solution:
                    wanted = list(solution[:2])
                    ready = mortar[1]["cooldown"] <= self.step / 2
                    fire[mortar[0]] = ready and on_target(slew(current, wanted, stats), wanted,
                                                          target["distance"], 3.0, 3.0)
                    return wanted, fire, reload
            return self.look(target), fire, reload

        point, track = target["point"], target["track"]
        distance = math.dist(muzzle, point)
        width, height = target["radius"], target["half_height"]
        origin = [muzzle[0] + 3 * math.cos(current[0]), muzzle[1] + 3 * math.sin(current[0]), muzzle[2]]

        # Work out the correct aim for each weapon, then pick which one gets the torso.
        solutions = {}
        if gun:
            spot = lead(origin, point, track, gun[1]["stats"]["speed"], self.step / 2)
            if math.dist(origin, spot) < gun[1]["stats"]["range"] and self.arena.clear(muzzle, spot):
                solutions["gun"] = angles(muzzle, spot)
        if laser and distance < laser[1]["stats"]["range"]:
            spot = [p + s for p, s in zip(point, track.predict(self.step / 2))]
            solutions["laser"] = angles(muzzle, spot)
        lightning = self.slots.get("lightning")
        if lightning and distance < lightning[1]["stats"]["range"] + target["radius"]:
            solutions["lightning"] = angles(muzzle, point)
        mortar = self.slots.get("mortar")
        if mortar and target["mech"]:
            solution = self.mortar_solution(target, mortar[1])
            if solution:
                solutions["mortar"] = list(solution[:2])

        gun_ready = bool(gun) and not gun[1].get("reload") and gun[1].get("ammo", 1) > 0
        laser_due = bool(laser) and "laser" in solutions and charge >= laser[1]["stats"]["charge"] - 2 * self.step
        mortar_due = "mortar" in solutions and mortar[1]["cooldown"] < 0.25
        if laser_due:
            main = "laser"
        elif mortar_due:
            main = "mortar"
        elif gun_ready and "gun" in solutions:
            main = "gun"
        else:
            main = next((k for k in ("laser", "lightning", "gun") if k in solutions), None)
        aim = solutions[main] if main else angles(muzzle, point)
        actual = slew(current, aim, stats)  # judge shots by where the torso will really point

        if "gun" in solutions and gun_ready:
            spread = math.radians(gun[1]["stats"]["spread_deg"]) * distance
            worth_it = spread < width * 1.1 or not gun[1].get("ammo") or target["mech"]
            fire[gun[0]] = worth_it and on_target(actual, solutions["gun"], distance, width * 0.8, height * 0.8)

        if laser:
            slot, weapon = laser
            spec = weapon["stats"]
            if charge:
                full = charge + self.step / 2 >= spec["charge"]
                expiring = charge + self.step >= spec["charge"] + spec["hold_s"]
                locked = "laser" in solutions and on_target(actual, solutions["laser"], distance,
                                                            width * 0.75, height * 0.75)
                finisher = self.kills_now(target, spec, charge)
                release = expiring or (locked and (full or finisher))
                fire[slot] = not release
            elif "laser" in solutions and weapon["cooldown"] <= self.step / 2:
                # Start charging while still turning onto the target.
                fire[slot] = abs(wrap(current[0] - solutions["laser"][0])) < 1.2

        if "lightning" in solutions:
            slot, weapon = lightning
            off = abs(wrap(actual[0] - solutions["lightning"][0]))
            enough = self.me["energy"] >= weapon["stats"]["energy"]
            fire[slot] = enough and off < math.radians(weapon["stats"]["cone_deg"]) * 0.45

        if mortar_due:
            fire[mortar[0]] = on_target(actual, solutions["mortar"], distance, width, height)

        return aim, fire, False

    def kills_now(self, target, spec, charge):
        hp = target["info"].get("hp")
        if hp is None or charge < spec["min_charge"]:
            return False
        part = min(1.0, charge / spec["charge"])
        damage = spec["damage"] * (spec["min_frac"] + (1 - spec["min_frac"]) * part)
        return hp <= damage * 0.9

    def mortar_solution(self, target, weapon):
        spec = weapon["stats"]
        if target["distance"] < spec["splash_radius"] * 3:
            return None  # would splash ourselves
        x, y, z = target["info"]["pos"]
        origin = [self.pos[0], self.pos[1], self.muzzle[2] + 2.5]
        solution = lob(origin, [x, y, z + 1.5], target["track"], spec["speed"], spec["gravity"])
        if not solution or solution[2] > 2.5:
            return None
        points = arc_points(origin, solution, spec["speed"], spec["gravity"])
        points[-1] = (points[-1][0], points[-1][1], points[-1][2] + 1.0)
        if all(self.arena.clear(a, b) for a, b in zip(points, points[1:])):
            return solution
        return None

    def want_reload(self, gun, target):
        if not gun or gun[1].get("reload"):
            return False
        ammo, size = gun[1].get("ammo"), gun[1]["stats"]["mag"]
        calm = not target or not target["mech"] or target["distance"] > 60
        return ammo is not None and calm and ammo < size * 0.5

    def look(self, target):
        if target and target["mech"]:
            x, y, z = target["info"]["pos"]
            return angles(self.muzzle, [x, y, z + target["height"] / 2])
        # Crates give cubes and may hold a laser.
        crate = self.crate_shot()
        if crate:
            return angles(self.muzzle, crate)
        if self.alert and self.t - self.alert[1] < 1.2:
            return [self.alert[0], 0.0]
        # Keep turning: we only see what we face, and that is how lasers get found.
        return [wrap(self.me["aim"][0] + 0.8 * self.stats["turn_rate"] * self.step), 0.0]

    def crate_shot(self):
        for crate in sorted(self.obs["crates"], key=lambda c: math.dist(c["pos"], self.pos))[:3]:
            x, y, z = crate["pos"]
            point = [x, y, z + 1.0]
            if math.dist(crate["pos"], self.pos) < 120 and self.arena.clear(self.muzzle, [x, y, z + 1.5]):
                return point
        return None

    def shoot_crate(self, aim, fire):
        crate, gun = self.crate_shot(), self.slots.get("gun")
        if crate and gun and not gun[1].get("reload"):
            distance = math.dist(self.muzzle, crate)
            actual = slew(self.me["aim"], aim, self.stats)
            if on_target(actual, angles(self.muzzle, crate), distance, 1.2, 1.2):
                fire[gun[0]] = True

    # Loot and upgrades

    def weapon_to_take(self):
        reach = self.config["loot"]["pickup_radius"]
        best = None
        for item in self.obs["pickups"]:
            if item["kind"] in self.slots or self.skipped.get(item["id"], 0) > self.t:
                continue
            if flat_distance(item["pos"], self.pos) <= reach and abs(item["pos"][2] - self.pos[2]) <= 4:
                if not best or WEAPON_RANK[item["kind"]] > WEAPON_RANK[best["kind"]]:
                    best = item
        if not best:
            return {}
        weapons = self.me["weapons"]
        if None in weapons:
            return {"take": best["id"]}
        if best["kind"] == "laser":
            # Keep the gun: it fills the gaps between beams.
            slot = min((i for i, w in enumerate(weapons) if w["kind"] != "gun"),
                       key=lambda i: WEAPON_RANK[weapons[i]["kind"]], default=None)
            if slot is not None:
                return {"take": best["id"], "drop": slot}
        return {}

    def pick_upgrade(self):
        offer = self.obs.get("offer")
        if not offer:
            return {}

        def rank(index):
            name = offer["choices"][index]["id"]
            return UPGRADES.index(name) if name in UPGRADES else len(UPGRADES)

        return {"pick": min(range(len(offer["choices"])), key=rank)}

    def act(self, obs):
        self.observe(obs)
        target = self.choose_target()
        self.update_retreat(target)
        goal, hurry = self.choose_goal(target)
        move, boost, jump = self.steer(goal, hurry)
        aim, fire, reload = self.shoot(target)
        if not target:
            self.shoot_crate(aim, fire)
        aim = [wrap(aim[0]), clamp(aim[1], self.stats["min_pitch"], self.stats["max_pitch"])]
        action = {"move": move, "boost": boost, "jump": jump, "aim": aim,
                  "fire": fire, "reload": reload and not any(fire)}
        action.update(self.weapon_to_take())
        action.update(self.pick_upgrade())
        return action


if __name__ == "__main__":
    mf.run(Professor)
