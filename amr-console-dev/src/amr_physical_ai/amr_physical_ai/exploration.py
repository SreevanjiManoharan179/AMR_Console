"""Autonomous exploration on a 2D occupancy grid: frontiers, information gain and the next goal.

Pure NumPy and SciPy, no ROS. The live explorer node and the offline grid simulator call the same
functions, so each method behaves the same in both. Grid convention as command_pipeline.Grid:
data[row, col] with row 0 at the lowest y; -1 unknown, 0 free, 100 occupied. SLAM Toolbox and the
map server publish only these; any other value counts as occupied from 65 up and as unknown below.

The three benchmarked methods share every step except the choice of the next goal:
  nearest  the frontier with the shortest path (Explore-Bench "Cost"), from its best viewpoint
  gain     the viewpoint with the most unseen area per metre of path (+ a fixed cost per goal)
  ours     gain's rule on the large frontiers first; then the small ones (corner sweep), each from its
           best viewpoint, in shortest-tour order; then home, so SLAM closes its loop
"""
from __future__ import annotations

from dataclasses import dataclass, field
import itertools
import hashlib
import math

import numpy as np
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra

UNKNOWN, FREE, OCCUPIED = -1, 0, 100
METHODS = ('nearest', 'gain', 'ours')


@dataclass(frozen=True)
class Settings:
    """Chosen before any run and frozen before the scored runs; each value has its reason."""
    clearance_m: float = 0.27          # robot radius 0.22 m (nav2_mppi.yaml) plus one 0.05 m cell
    sensor_range_m: float = 20.0       # matches installed gz_waffle.sdf.xacro ray/range/max (20.0 m)
    rays: int = 180                    # 2 degree spacing: every second simulated beam
    min_frontier_cells: int = 3        # a frontier shorter than 0.15 m is a speck of noise
    large_frontier_cells: int = 10     # 0.5 m of frontier: an open side or a doorway
    viewpoint_radius_m: float = 1.0    # a viewpoint stands within 1 m of its frontier
    viewpoints_per_frontier: int = 8   # candidates scored per frontier, spread by path length
    min_gain_cells: int = 1            # a viewpoint must expect to see at least one unknown cell
    goal_cost_m: float = 1.0           # each goal costs a stop and a turn, counted as 1 m of path
    snap_radius_m: float = 0.6         # the robot's start may sit this far from the clear floor
    blacklist_radius_m: float = 0.5    # no goals near one Nav2 failed to reach
    give_up_radius_m: float = 0.5      # a frontier still open after two visits is not chosen again
    give_up_visits: int = 2            # the second visit comes from a different viewpoint
    home_tolerance_m: float = 0.3      # closer than this to home counts as home
    exact_tour_limit: int = 7          # tours over at most this many stops are solved exactly
    min_goal_step_m: float = 0.6       # prefer motion beyond the mapper's 0.5 m travel threshold
    unchanged_cluster_visits: int = 2 # exclude a repeatedly unobserved cluster until the map changes


@dataclass(frozen=True)
class Map:
    data: np.ndarray            # [rows, cols], row 0 at the lowest y
    resolution: float
    origin: tuple               # (x, y) of the lower-left corner of cell (0, 0); yaw 0 only

    def cell(self, x, y):
        return (math.floor((y - self.origin[1]) / self.resolution),
                math.floor((x - self.origin[0]) / self.resolution))

    def point(self, row, col):
        return (self.origin[0] + (col + .5) * self.resolution, self.origin[1] + (row + .5) * self.resolution)

    def inside(self, row, col):
        return 0 <= row < self.data.shape[0] and 0 <= col < self.data.shape[1]


@dataclass
class Decision:
    kind: str                   # 'frontier', 'home' or 'done'
    x: float = 0.
    y: float = 0.
    yaw: float = 0.             # a frontier goal's approach heading (approach_yaw); 0 at home
    method: str = ''
    path_m: float = 0.
    gain_m2: float = 0.
    frontier_xy: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))   # the target frontier, map frame
    frontiers_left: int = 0
    phase: str = ''
    reason: str = ''


def map_signature(grid):
    """Content identity, independent of a republished OccupancyGrid timestamp."""
    return (grid.resolution, tuple(grid.origin), grid.data.shape,
            hashlib.sha256(np.asarray(grid.data, dtype=np.int16).tobytes()).digest())


def frontier_key(points):
    """Stable cluster identity on one unchanged map; no proximity merging of distinct clusters."""
    return hashlib.sha256(np.asarray(points, dtype=np.float64).tobytes()).digest()


def masks(data):
    """(free, occupied, unknown) boolean masks of a grid."""
    data = np.asarray(data)
    free = data == FREE
    occupied = data >= 65
    return free, occupied, ~(free | occupied)


def frontier_mask(free, unknown):
    """Free cells with an unknown 4-neighbour; beyond the grid's edge counts as unknown."""
    pad = np.pad(unknown, 1, constant_values=True)
    near = pad[:-2, 1:-1] | pad[2:, 1:-1] | pad[1:-1, :-2] | pad[1:-1, 2:]
    return free & near


def frontier_clusters(free, unknown, min_cells):
    """Label 8-connected frontier groups; returns (labels, {label: size}) for groups >= min_cells."""
    labels, count = ndimage.label(frontier_mask(free, unknown), structure=np.ones((3, 3), dtype=bool))
    sizes = np.bincount(labels.ravel(), minlength=count + 1)
    return labels, {k: int(sizes[k]) for k in range(1, count + 1) if sizes[k] >= min_cells}


def still_open(frontier_xy, grid):
    """How many of a frontier's points are still frontier cells on this (possibly regrown) map, and their centre."""
    if len(frontier_xy) == 0:
        return 0, None
    free, _, unknown = masks(grid.data)
    rows = np.floor((frontier_xy[:, 1] - grid.origin[1]) / grid.resolution).astype(np.int64)
    cols = np.floor((frontier_xy[:, 0] - grid.origin[0]) / grid.resolution).astype(np.int64)
    inside = (rows >= 0) & (rows < free.shape[0]) & (cols >= 0) & (cols < free.shape[1])
    hit = np.zeros(len(frontier_xy), dtype=bool)
    hit[inside] = frontier_mask(free, unknown)[rows[inside], cols[inside]]
    return int(hit.sum()), (tuple(frontier_xy[hit].mean(axis=0)) if hit.any() else None)


def clearance(free, resolution):
    """Metres from each free cell to the nearest cell that is not known free (as command_pipeline.Grid)."""
    return ndimage.distance_transform_edt(np.pad(free, 1, constant_values=False))[1:-1, 1:-1] * resolution


class Paths:
    """Shortest paths over the clear floor (8-connected), from one start cell."""

    def __init__(self, passable, resolution, start):
        self.shape = passable.shape
        self.index = np.full(passable.shape, -1, dtype=np.int64)
        self.cells = np.flatnonzero(passable)
        self.index.ravel()[self.cells] = np.arange(self.cells.size)
        rows, cols = passable.shape
        src, dst, cost = [], [], []
        for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):    # each undirected edge once
            ra, rb = slice(0, rows - dr), slice(dr, rows)
            ca, cb = (slice(0, cols - dc), slice(dc, cols)) if dc >= 0 else (slice(-dc, cols), slice(0, cols + dc))
            both = passable[ra, ca] & passable[rb, cb]
            src.append(self.index[ra, ca][both])
            dst.append(self.index[rb, cb][both])
            cost.append(np.full(int(both.sum()), resolution * (math.sqrt(2.) if dr and dc else 1.)))
        n = self.cells.size
        self.graph = coo_matrix((np.concatenate(cost), (np.concatenate(src), np.concatenate(dst))), shape=(n, n)).tocsr()
        self.start = start
        node = self.index[start]
        distance, self.predecessor = dijkstra(self.graph, directed=False, indices=node, return_predecessors=True)
        self.distance = np.full(passable.shape, np.inf)
        self.distance.ravel()[self.cells] = distance

    def between(self, starts, ends):
        """Path metres from each start cell to each end cell, as a [len(starts), len(ends)] array."""
        nodes = [self.index[s] for s in starts]
        table = dijkstra(self.graph, directed=False, indices=nodes)
        return table[:, [self.index[e] for e in ends]]

    def path(self, goal):
        """Cells from the start to goal, inclusive, or [] if unreachable."""
        node, start = self.index[goal], self.index[self.start]
        if node < 0 or not np.isfinite(self.distance[goal]):
            return []
        out = []
        while node != start and node >= 0:
            out.append(np.unravel_index(self.cells[node], self.shape))
            node = self.predecessor[node]
        out.append(self.start)
        return [tuple(int(v) for v in c) for c in reversed(out)]


def snap(passable, cell, radius_cells):
    """The clear cell nearest to cell within radius (the cell itself if clear), or None."""
    row, col = cell
    if 0 <= row < passable.shape[0] and 0 <= col < passable.shape[1] and passable[row, col]:
        return (row, col)
    r0, r1 = max(0, row - radius_cells), min(passable.shape[0], row + radius_cells + 1)
    c0, c1 = max(0, col - radius_cells), min(passable.shape[1], col + radius_cells + 1)
    rows, cols = np.nonzero(passable[r0:r1, c0:c1])
    if rows.size == 0:
        return None
    d2 = (rows + r0 - row) ** 2 + (cols + c0 - col) ** 2
    best = int(np.argmin(d2))
    if d2[best] > radius_cells ** 2:
        return None
    return (int(rows[best] + r0), int(cols[best] + c0))


_RAYS = {}


def ray_offsets(rays, range_cells):
    """Cell offsets of each ray's half-cell steps from a cell's centre: (rows, cols), [rays, steps].
    floor(r + 0.5 + s sin a) = r + floor(0.5 + s sin a) for a whole cell r, so one table serves every cell."""
    key = (rays, range_cells)
    if key not in _RAYS:
        angles = np.linspace(0., 2 * math.pi, rays, endpoint=False)
        steps = np.arange(1, range_cells * 2 + 1) * .5                   # half-cell steps
        _RAYS[key] = (np.floor(.5 + np.outer(np.sin(angles), steps)).astype(np.int32),
                      np.floor(.5 + np.outer(np.cos(angles), steps)).astype(np.int32))
    return _RAYS[key]


def visible_unknown(occupied, unknown, cell, resolution, settings, chunk=40):
    """Unknown cells a full scan from cell would see; unknown cells are taken as see-through.
    Rays are followed a metre (40 half-cell steps) at a time and dropped once blocked."""
    rows, cols = occupied.shape
    dr, dc = ray_offsets(settings.rays, int(settings.sensor_range_m / resolution))
    occ, unk = occupied.ravel(), unknown.ravel()
    seen = np.zeros(rows * cols, dtype=bool)
    active = np.arange(dr.shape[0])
    for first in range(0, dr.shape[1], chunk):
        rr, cc = cell[0] + dr[active, first:first + chunk], cell[1] + dc[active, first:first + chunk]
        inside = (rr >= 0) & (rr < rows) & (cc >= 0) & (cc < cols)
        flat = np.where(inside, rr * cols + cc, 0)
        clear = np.cumsum(~inside | occ[flat], axis=1) == 0     # before the first wall or the grid's edge
        seen[flat[clear & unk[flat]]] = True
        active = active[clear[:, -1]]
        if active.size == 0:
            break
    return np.flatnonzero(seen)


def _disk(radius):
    yy, xx = np.mgrid[-radius:radius + 1, -radius:radius + 1]
    return yy * yy + xx * xx <= radius * radius


def viewpoints(labels, cluster, reachable, distance, resolution, settings, banned):
    """Up to N reachable clear cells within the viewpoint radius of a frontier, spread by path length."""
    radius = max(1, int(round(settings.viewpoint_radius_m / resolution)))
    rows, cols = np.nonzero(labels == cluster)
    r0, r1 = max(0, rows.min() - radius), min(labels.shape[0], rows.max() + radius + 1)
    c0, c1 = max(0, cols.min() - radius), min(labels.shape[1], cols.max() + radius + 1)
    # Cells within the radius of the frontier (the same set as a dilation by a disk of that radius).
    near = ndimage.distance_transform_edt(labels[r0:r1, c0:c1] != cluster) <= radius
    near &= reachable[r0:r1, c0:c1] & ~banned[r0:r1, c0:c1]
    cr, cc = np.nonzero(near)
    if cr.size == 0:
        return []
    cr, cc = cr + r0, cc + c0
    order = np.argsort(distance[cr, cc], kind='stable')
    pick = order[np.unique(np.linspace(0, order.size - 1, min(settings.viewpoints_per_frontier, order.size)).astype(int))]
    return [(int(cr[i]), int(cc[i])) for i in pick]


def approach_yaw(paths, goal, resolution, fallback, back_m=.5):
    """Heading of the last back_m of the shortest path to goal. The LiDAR sees all round, so the robot
    need not turn at a viewpoint; with no path to speak of, the heading of fallback (dx, dy) is used."""
    path = paths.path(goal)
    if len(path) >= 2:
        (r0, c0), (r1, c1) = path[max(0, len(path) - 1 - int(round(back_m / resolution)))], path[-1]
        return math.atan2(r1 - r0, c1 - c0)
    return math.atan2(fallback[1], fallback[0])


def shortest_tour(start_row, table, exact_limit=Settings.exact_tour_limit):
    """Order of stops for the shortest open path from the start (start_row: costs from the start)."""
    n = len(start_row)
    if n <= 1:
        return list(range(n))
    if n <= exact_limit:
        best, order = math.inf, None
        for perm in itertools.permutations(range(n)):
            cost = start_row[perm[0]] + sum(table[perm[i], perm[i + 1]] for i in range(n - 1))
            if cost < best:
                best, order = cost, list(perm)
        return order
    left, order, here = set(range(n)), [], None           # nearest neighbour, then 2-opt
    while left:
        nxt = min(left, key=lambda j: start_row[j] if here is None else table[here, j])
        order.append(nxt)
        left.remove(nxt)
        here = nxt

    def length(o):
        return start_row[o[0]] + sum(table[o[i], o[i + 1]] for i in range(len(o) - 1))

    improved = True
    while improved:
        improved = False
        for i in range(n - 1):
            for j in range(i + 1, n):
                trial = order[:i] + order[i:j + 1][::-1] + order[j + 1:]
                if length(trial) + 1e-9 < length(order):
                    order, improved = trial, True
    return order


class Explorer:
    """Chooses the next goal from the current map; holds only the blacklist and the phase."""

    def __init__(self, method, settings=Settings(), home=(0., 0.)):
        if method not in METHODS:
            raise ValueError(f'Unknown exploration method {method!r}; choose one of {", ".join(METHODS)}.')
        self.method, self.settings, self.home = method, settings, home
        self.failed = []                # (x, y) of goals Nav2 could not reach
        self.given_up = []              # (x, y) centres of frontiers still open after repeated visits
        self.open_visits = []           # (x, y) centres of frontiers still open after one visit
        self.phase = 'explore'          # ours: explore -> sweep -> home -> done
        self.unchanged_views = []       # successful attempts without changed map content
        self.unchanged_clusters = {}
        self.short_goal_used = False
        self.map_identity = None
        self.home_sent = False          # ours sends the home goal once, reached or not

    def report_failure(self, x, y):
        """Nav2 could not reach this goal: no viewpoint near it again."""
        self.failed.append((float(x), float(y)))

    def report_arrival(self, decision, grid, *, map_changed=True):
        """After reaching a frontier goal. A frontier still open is tried once more from another
        viewpoint (this one is banned); still open after that, it is given up, so no method loops."""
        if decision.kind != 'frontier':
            return False
        count, centre = still_open(decision.frontier_xy, grid)
        if count < self.settings.min_frontier_cells:
            return False
        if not map_changed:
            # A newer publication of the same grid is not evidence of a new
            # observation at this goal. Exclude only this attempted cell;
            # do not turn success into a 0.5 m failure disk or abandon a cluster.
            self.unchanged_views.append((decision.x, decision.y))
            key = frontier_key(decision.frontier_xy)
            self.unchanged_clusters[key] = self.unchanged_clusters.get(key, 0) + 1
            return True
        self.failed.append((decision.x, decision.y))
        visits = 1 + sum(math.dist(centre, c) <= self.settings.give_up_radius_m for c in self.open_visits)
        self.open_visits.append(centre)
        if visits >= self.settings.give_up_visits:
            self.given_up.append(centre)
        return True

    def _banned(self, grid):
        banned = np.zeros(grid.data.shape, dtype=bool)
        radius = int(round(self.settings.blacklist_radius_m / grid.resolution))
        for x, y in self.failed:
            row, col = grid.cell(x, y)
            r0, c0 = row - radius, col - radius
            disk = _disk(radius)
            rr, cc = np.nonzero(disk)
            rr, cc = rr + r0, cc + c0
            ok = (rr >= 0) & (rr < banned.shape[0]) & (cc >= 0) & (cc < banned.shape[1])
            banned[rr[ok], cc[ok]] = True
        for x, y in self.unchanged_views:
            cell = grid.cell(x, y)
            if grid.inside(*cell):
                banned[cell] = True
        return banned

    def next_goal(self, grid, robot_xy):
        s, res = self.settings, grid.resolution
        identity = map_signature(grid)
        if identity != self.map_identity:
            self.unchanged_views.clear()
            self.unchanged_clusters.clear()
            self.short_goal_used = False
            self.map_identity = identity
        free, occupied, unknown = masks(grid.data)
        passable = clearance(free, res) >= s.clearance_m
        start = snap(passable, grid.cell(*robot_xy), int(round(s.snap_radius_m / res)))
        if start is None:
            return Decision('done', method=self.method, phase=self.phase,
                            reason='The robot is not on clear floor; no path can start here.')
        paths = Paths(passable, res, start)
        reachable = np.isfinite(paths.distance)
        labels, clusters = frontier_clusters(free, unknown, s.min_frontier_cells)
        for k in list(clusters):
            rows, cols = np.nonzero(labels == k)
            points = np.column_stack([grid.origin[0] + (cols + .5) * res,
                                      grid.origin[1] + (rows + .5) * res])
            if self.unchanged_clusters.get(frontier_key(points), 0) >= s.unchanged_cluster_visits:
                del clusters[k]
        banned = self._banned(grid)
        if self.given_up:
            centres = ndimage.center_of_mass(np.ones_like(labels), labels, list(clusters))
            for k, (row, col) in zip(list(clusters), centres):
                cx, cy = grid.point(row, col)
                if any(math.dist((cx, cy), g) <= s.give_up_radius_m for g in self.given_up):
                    del clusters[k]
        views = {k: viewpoints(labels, k, reachable, paths.distance, res, s, banned) for k in clusters}
        views = {k: v for k, v in views.items() if v}
        cache = {}

        def gain(view):                  # unseen area a scan from the viewpoint would show, m2
            if view not in cache:
                cache[view] = visible_unknown(occupied, unknown, view, res, s).size * res * res
            return cache[view]

        def worth(ks):                   # a goal must be expected to show something new
            found = {k: [v for v in views[k] if gain(v) >= s.min_gain_cells * res * res - 1e-12] for k in ks}
            return {k: v for k, v in found.items() if v}

        # The minimum step and one short fallback apply before any method's
        # ranking, including ours' explore/sweep split.
        views = worth(views)
        far = {k: [v for v in vs if paths.distance[v] >= s.min_goal_step_m - 1e-9]
               for k, vs in views.items()}
        far = {k: vs for k, vs in far.items() if vs}
        if far:
            views = far
        elif views and not self.short_goal_used:
            k, view = min(((k, v) for k, vs in views.items() for v in vs),
                          key=lambda kv: paths.distance[kv[1]])
            self.short_goal_used = True
            return self._decision(grid, labels, k, view, paths, gain(view), len(views))
        else:
            return self._finish(grid, robot_xy, paths, passable)
        if self.method == 'nearest':     # the closest frontier worth a visit, seen from its best viewpoint
            for k in sorted(views, key=lambda c: min(paths.distance[v] for v in views[c])):
                options = worth([k]).get(k)
                if options:
                    view = max(options, key=lambda v: (gain(v), -paths.distance[v]))
                    return self._decision(grid, labels, k, view, paths, gain(view), len(views))
            return self._finish(grid, robot_xy, paths, passable)
        if self.method == 'ours':        # the large frontiers first; the small ones in the corner sweep
            options = worth([k for k in views if clusters[k] >= s.large_frontier_cells])
            self.phase = 'explore' if options else 'sweep'
            options = options or worth([k for k in views if clusters[k] < s.large_frontier_cells])
        else:
            options = worth(views)
        if not options:
            return self._finish(grid, robot_xy, paths, passable)
        utility = {(k, v): gain(v) / (paths.distance[v] + s.goal_cost_m) for k, vs in options.items() for v in vs}
        if self.method == 'gain' or self.phase == 'explore':
            k, view = max(utility, key=lambda kv: (utility[kv], -paths.distance[kv[1]]))
            return self._decision(grid, labels, k, view, paths, gain(view), len(views))
        best = {}                        # ours, corner sweep: the best viewpoint of each frontier, then the tour
        for (k, v), u in utility.items():
            if k not in best or u > best[k][1]:
                best[k] = (v, u)
        stops = sorted(best)
        stop_views = [best[k][0] for k in stops]
        start_row = np.array([paths.distance[v] for v in stop_views])
        table = paths.between(stop_views, stop_views) if len(stop_views) > 1 else np.zeros((1, 1))
        k = stops[shortest_tour(start_row, table, s.exact_tour_limit)[0]]
        return self._decision(grid, labels, k, best[k][0], paths, gain(best[k][0]), len(views))

    def _decision(self, grid, labels, cluster, view, paths, gain, left):
        rows, cols = np.nonzero(labels == cluster)
        x, y = grid.point(*view)
        fx, fy = grid.point(rows.mean(), cols.mean())
        points = np.column_stack([grid.origin[0] + (cols + .5) * grid.resolution,
                                  grid.origin[1] + (rows + .5) * grid.resolution])
        return Decision('frontier', x, y, approach_yaw(paths, view, grid.resolution, (fx - x, fy - y)),
                        self.method, float(paths.distance[view]), float(gain), points, left, self.phase,
                        f'frontier of {rows.size} cells')

    def _finish(self, grid, robot_xy, paths, passable):
        """No reachable frontier left: ours returns home once (loop closure); the baselines stop.
        A home goal that fails is not sent again."""
        if self.unchanged_views:
            return Decision('done', method=self.method, phase=self.phase,
                            reason='stalled: no eligible frontier cluster remains on the unchanged map')
        if self.method == 'ours' and not self.home_sent:
            self.home_sent = True
            far = math.dist(robot_xy, self.home) > self.settings.home_tolerance_m
            home = snap(passable, grid.cell(*self.home), int(round(self.settings.snap_radius_m / grid.resolution)))
            if far and home is not None and np.isfinite(paths.distance[home]):
                self.phase = 'home'
                return Decision('home', *self.home, 0., self.method, float(paths.distance[home]), 0.,
                                phase='home', reason='no frontier left: return home along the corridor')
        self.phase = 'done'
        return Decision('done', method=self.method, phase='done',
                        reason='stalled: no eligible frontier cluster remains')
