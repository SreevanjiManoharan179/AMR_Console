#!/usr/bin/env python3
"""Score autonomous exploration against the exact map (the metrics of the exploration benchmark design).

Offline only: no ROS, no simulator. Every function works on arrays with row 0 at the lowest y and
origins as (x, y); a SLAM map may have any origin and size, the exact map is the ruler.

  explored ratio   share of the exact map's free cells the SLAM map knows (free or occupied)
  T90, T99         first time the explored ratio reaches 90% and 99%
  distance to 99%  metres driven by then
  coverage AUC     mean explored ratio over the first 6 minutes
  walls, faces     office_world.compare_layers (the office-world map check): SLAM walls on exact walls, faces seen
  corners seen     room and furniture corners whose wall cells are all on the SLAM map
  floor IoU        SLAM free cells against the exact map's free cells, cell by cell (1 for a perfect
                   map; occupied cells are not compared, since SLAM marks only wall faces)
"""
from __future__ import annotations

import math
from pathlib import Path
import sys

import numpy as np
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parent))
import office_world  # noqa: E402

UNKNOWN, FREE = -1, 0
AUC_HORIZON_S = 360.


def on_exact(data, res, origin, shape, e_res, e_origin):
    """A grid's values at the centre of every exact-map cell (nearest cell); unknown outside the grid."""
    rows, cols = shape
    xs = e_origin[0] + (np.arange(cols) + .5) * e_res
    ys = e_origin[1] + (np.arange(rows) + .5) * e_res
    c = np.floor((xs - origin[0]) / res).astype(np.int64)
    r = np.floor((ys - origin[1]) / res).astype(np.int64)
    out = np.full(shape, UNKNOWN, dtype=np.int16)
    rv, cv = (r >= 0) & (r < data.shape[0]), (c >= 0) & (c < data.shape[1])
    out[np.ix_(rv, cv)] = np.asarray(data)[np.ix_(r[rv], c[cv])]
    return out


def explored_ratio(values_on_exact, e_free):
    known = values_on_exact != UNKNOWN
    return float(np.count_nonzero(known & e_free) / np.count_nonzero(e_free))


def first_reaching(series, level):
    """(time, distance) of the first sample at or above level; (None, None) if never."""
    for sample in series:
        if sample[1] >= level - 1e-12:
            return sample[0], (sample[2] if len(sample) > 2 else None)
    return None, None


def coverage_auc(series, horizon_s=AUC_HORIZON_S):
    """Mean explored ratio over [0, horizon]; each value holds until the next sample, the last one after."""
    times = np.array([s[0] for s in series], dtype=float)
    values = np.array([s[1] for s in series], dtype=float)
    edges = np.clip(np.append(times, horizon_s), 0., horizon_s)
    widths = np.diff(edges)
    return float(np.sum(values * widths) / horizon_s)


def surface(e_occ, e_free):
    padded = np.pad(e_free, 1, constant_values=False)
    return e_occ & (padded[:-2, 1:-1] | padded[2:, 1:-1] | padded[1:-1, :-2] | padded[1:-1, 2:])


def corner_sites(e_occ, e_free):
    """Labelled corners: free cells walled on both axes (concave) and wall cells free round a corner (convex)."""
    o = np.pad(e_occ, 1, constant_values=False)
    f = np.pad(e_free, 1, constant_values=False)
    up, down, left, right = (slice(2, None), slice(1, -1)), (slice(0, -2), slice(1, -1)), \
        (slice(1, -1), slice(0, -2)), (slice(1, -1), slice(2, None))
    concave = e_free & (o[up] | o[down]) & (o[left] | o[right])
    quadrant = np.zeros_like(e_occ)
    for dr in (0, 2):
        for dc in (0, 2):
            diagonal = f[dr:dr + e_occ.shape[0], dc:dc + e_occ.shape[1]]
            vertical = f[dr:dr + e_occ.shape[0], 1:-1]
            horizontal = f[1:-1, dc:dc + e_occ.shape[1]]
            quadrant |= diagonal & vertical & horizontal
    convex = e_occ & quadrant
    return ndimage.label(concave | convex, structure=np.ones((3, 3), dtype=bool))


def corners_seen(s_occ, s_res, s_origin, e_occ, e_free, e_res, e_origin, tolerance=.10):
    """Corners whose wall cells (within one cell of the corner) are all within tolerance of a SLAM wall."""
    labels, count = corner_sites(e_occ, e_free)
    field = office_world._distance_field(s_occ, s_res)
    seen = 0
    for k, box in enumerate(ndimage.find_objects(labels), start=1):
        r0, r1 = max(0, box[0].start - 1), min(labels.shape[0], box[0].stop + 1)
        c0, c1 = max(0, box[1].start - 1), min(labels.shape[1], box[1].stop + 1)
        walls = ndimage.binary_dilation(labels[r0:r1, c0:c1] == k, structure=np.ones((3, 3), dtype=bool))
        walls &= e_occ[r0:r1, c0:c1]
        rows, cols = np.nonzero(walls)
        xs = e_origin[0] + (cols + c0 + .5) * e_res
        ys = e_origin[1] + (rows + r0 + .5) * e_res
        if rows.size and np.all(office_world._sample(field, s_res, s_origin, xs, ys) <= tolerance + 1e-9):
            seen += 1
    return {'corners': int(count), 'seen': seen, 'ratio': round(seen / count, 4) if count else None}


def floor_iou(values_on_exact, e_free):
    mapped = values_on_exact == FREE
    union = np.count_nonzero(mapped | e_free)
    return round(np.count_nonzero(mapped & e_free) / union, 4) if union else None


def score_map(data, res, origin, e_occ, e_free, e_res, e_origin, tolerance=.10):
    """Map-quality scores of one occupancy grid (-1 / 0 / 100 values) against the exact map."""
    data = np.asarray(data)
    values = on_exact(data, res, origin, e_free.shape, e_res, e_origin)
    s_occ = data >= 65
    walls = office_world.compare_layers(s_occ, res, origin, e_occ, e_free, e_res, e_origin, tolerance)
    return {'explored_ratio': round(explored_ratio(values, e_free), 4),
            'wall_agreement': walls['slam_occupied_on_exact_walls']['within_tolerance'],
            'faces_seen': walls['exact_faces_seen_by_slam']['within_tolerance'],
            'corners': corners_seen(s_occ, res, origin, e_occ, e_free, e_res, e_origin, tolerance),
            'floor_iou': floor_iou(values, e_free)}


def score_series(series, horizon_s=AUC_HORIZON_S):
    """Time scores from (time s, explored ratio[, distance m]) samples."""
    t90, d90 = first_reaching(series, .90)
    t99, d99 = first_reaching(series, .99)
    return {'T90_s': t90, 'T99_s': t99, 'distance_to_90_m': d90, 'distance_to_99_m': d99,
            'coverage_auc_6min': round(coverage_auc(series, horizon_s), 4),
            'completion': round(series[-1][1], 4) if series else None}


def path_length(points):
    return float(sum(math.dist(a, b) for a, b in zip(points, points[1:])))
