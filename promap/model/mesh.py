"""Triangle meshes: game-engine style geometry for surfaces that are not one flat quad.

A Mesh is a vertex list and a triangle (index) list. Every vertex carries an output
position (x, y, pixels, y down) and a texture coordinate (u, v, 0..1, v=0 at the top
row of the media). Triangles that share a vertex *index* move together, so two
triangles sharing an edge make a quad, six make a hexagon, and so on. All triangles of
one mesh show the same media: the mesh is the "group".

Texture mapping inside a triangle is affine (the GPU interpolates u, v linearly), which
is exact when the triangle really is flat-on and a good approximation once a shape is
subdivided a few times. `quad_grid` builds a quad out of n x n cells with the texture
coordinates taken from the quad's homography, so a subdivided quad stays
perspective-correct to within the cell size.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from promap.model.geometry import homography_unit_square_to_quad

Point = tuple[float, float]


@dataclass
class Mesh:
    verts: list[list[float]] = field(default_factory=list)  # [x, y, u, v]
    tris: list[list[int]] = field(default_factory=list)  # [a, b, c] indices into verts

    # -- construction --------------------------------------------------------
    @classmethod
    def from_dict(cls, d: dict | None) -> "Mesh | None":
        if d is None:
            return None
        if isinstance(d, Mesh):
            return d
        return cls(verts=[[float(v) for v in row] for row in d.get("verts", [])],
                   tris=[[int(i) for i in t] for t in d.get("tris", [])])

    def copy(self) -> "Mesh":
        return Mesh(verts=[list(v) for v in self.verts], tris=[list(t) for t in self.tris])

    # -- queries ---------------------------------------------------------------
    def positions(self) -> list[list[float]]:
        return [[v[0], v[1]] for v in self.verts]

    def uvs(self) -> list[list[float]]:
        return [[v[2], v[3]] for v in self.verts]

    def bounds(self) -> tuple[float, float, float, float]:
        if not self.verts:
            return 0.0, 0.0, 0.0, 0.0
        xs = [v[0] for v in self.verts]
        ys = [v[1] for v in self.verts]
        return min(xs), min(ys), max(xs), max(ys)

    def bounds_corners(self) -> list[list[float]]:
        x0, y0, x1, y1 = self.bounds()
        return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]

    def centroid(self) -> Point:
        if not self.verts:
            return 0.0, 0.0
        return (sum(v[0] for v in self.verts) / len(self.verts), sum(v[1] for v in self.verts) / len(self.verts))

    def tri_points(self, i: int) -> list[list[float]]:
        return [[self.verts[k][0], self.verts[k][1]] for k in self.tris[i]]

    def outlines(self) -> list[list[list[float]]]:
        return [self.tri_points(i) for i in range(len(self.tris))]

    def hit(self, p: Point) -> int | None:
        """Index of the top-most triangle containing p, or None."""
        for i in range(len(self.tris) - 1, -1, -1):
            a, b, c = self.tri_points(i)
            if point_in_triangle(a, b, c, p):
                return i
        return None

    def nearest_edge(self, p: Point, tol: float) -> tuple[int, int] | None:
        """(tri, k) for the edge verts[tri[k]] -> verts[tri[(k+1)%3]] closest to p within tol."""
        best, best_d = None, tol
        for ti, t in enumerate(self.tris):
            for k in range(3):
                a = self.verts[t[k]]
                b = self.verts[t[(k + 1) % 3]]
                d = point_segment_distance(p, (a[0], a[1]), (b[0], b[1]))
                if d < best_d:
                    best, best_d = (ti, k), d
        return best

    def edge_vertices(self, edge: tuple[int, int]) -> tuple[int, int]:
        ti, k = edge
        t = self.tris[ti]
        return t[k], t[(k + 1) % 3]

    # -- edits (in place) ------------------------------------------------------
    def translate(self, dx: float, dy: float):
        for v in self.verts:
            v[0] += dx
            v[1] += dy

    def scaled_about(self, cx: float, cy: float, f: float) -> "Mesh":
        m = self.copy()
        for v in m.verts:
            v[0] = cx + (v[0] - cx) * f
            v[1] = cy + (v[1] - cy) * f
        return m

    def fit_uvs(self):
        """Stretch the media over the mesh's bounding box: u, v from x, y."""
        x0, y0, x1, y1 = self.bounds()
        w, h = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)
        for v in self.verts:
            v[2] = (v[0] - x0) / w
            v[3] = (v[1] - y0) / h

    def add_triangle(self, points, uvs=None) -> int:
        """Append a free triangle; returns its index."""
        if uvs is None:
            x0, y0, x1, y1 = self.bounds() if self.verts else (
                min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points))
            w, h = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)
            uvs = [[(p[0] - x0) / w, (p[1] - y0) / h] for p in points]
        base = len(self.verts)
        for p, uv in zip(points, uvs):
            self.verts.append([float(p[0]), float(p[1]), float(uv[0]), float(uv[1])])
        self.tris.append([base, base + 1, base + 2])
        return len(self.tris) - 1

    def extrude_edge(self, edge: tuple[int, int]) -> int:
        """Grow a new triangle off an edge, completing the parallelogram with the opposite vertex.

        Position and texture coordinate of the new vertex are both the affine extension
        (a + b - c), so the media continues seamlessly across the shared edge.
        """
        ti, k = edge
        t = self.tris[ti]
        ia, ib, ic = t[k], t[(k + 1) % 3], t[(k + 2) % 3]
        a, b, c = self.verts[ia], self.verts[ib], self.verts[ic]
        d = [a[j] + b[j] - c[j] for j in range(4)]
        self.verts.append(d)
        self.tris.append([ib, ia, len(self.verts) - 1])
        return len(self.tris) - 1

    def delete_triangle(self, i: int):
        del self.tris[i]
        self._drop_orphans()

    def detach(self, tri_indices) -> "Mesh":
        """Remove the given triangles from this mesh and return them as a new mesh."""
        keep = sorted(set(int(i) for i in tri_indices if 0 <= int(i) < len(self.tris)))
        out = Mesh(verts=[list(v) for v in self.verts], tris=[list(self.tris[i]) for i in keep])
        out._drop_orphans()
        self.tris = [t for i, t in enumerate(self.tris) if i not in keep]
        self._drop_orphans()
        return out

    def merge(self, other: "Mesh", tol: float = 0.5):
        """Append another mesh's triangles, welding vertices that coincide."""
        base = len(self.verts)
        self.verts += [list(v) for v in other.verts]
        self.tris += [[i + base for i in t] for t in other.tris]
        self.weld(tol)

    def weld(self, tol: float = 0.5):
        """Merge vertices closer than tol pixels; drop triangles that collapse."""
        remap = list(range(len(self.verts)))
        for i in range(len(self.verts)):
            if remap[i] != i:
                continue
            xi, yi = self.verts[i][0], self.verts[i][1]
            for j in range(i + 1, len(self.verts)):
                if remap[j] == j and abs(self.verts[j][0] - xi) <= tol and abs(self.verts[j][1] - yi) <= tol:
                    remap[j] = i
        self.tris = [[remap[i] for i in t] for t in self.tris]
        self.tris = [t for t in self.tris if len(set(t)) == 3]
        self._drop_orphans()

    def weld_vertex(self, i: int, j: int):
        """Make vertex j the same as vertex i (used when a dragged vertex snaps onto another)."""
        if i == j:
            return
        self.tris = [[i if k == j else k for k in t] for t in self.tris]
        self.tris = [t for t in self.tris if len(set(t)) == 3]
        self._drop_orphans()

    def subdivide(self):
        """Split every triangle into four at its edge midpoints (shared midpoints are reused)."""
        mids: dict[tuple[int, int], int] = {}

        def mid(i: int, j: int) -> int:
            key = (min(i, j), max(i, j))
            if key not in mids:
                a, b = self.verts[i], self.verts[j]
                self.verts.append([(a[k] + b[k]) / 2 for k in range(4)])
                mids[key] = len(self.verts) - 1
            return mids[key]

        new = []
        for a, b, c in self.tris:
            ab, bc, ca = mid(a, b), mid(b, c), mid(c, a)
            new += [[a, ab, ca], [ab, b, bc], [ca, bc, c], [ab, bc, ca]]
        self.tris = new

    def _drop_orphans(self):
        used = sorted({i for t in self.tris for i in t})
        remap = {old: new for new, old in enumerate(used)}
        self.verts = [self.verts[i] for i in used]
        self.tris = [[remap[i] for i in t] for t in self.tris]


# -- builders -----------------------------------------------------------------
def triangle_mesh(points, uvs=None) -> Mesh:
    m = Mesh()
    m.add_triangle(points, uvs)
    return m


def quad_grid(corners, n: int = 1) -> Mesh:
    """A quad as an n x n grid of cells (2 triangles each), perspective-correct per cell.

    Vertex positions come from the quad's homography evaluated at (u, v) = (i/n, j/n) and
    the texture coordinates are those (u, v), so the mesh reproduces the quad's warp to
    within one cell. n=1 is the plain two-triangle quad (affine, with the classic seam on
    a keystoned shape; use n=4 or more for anything not square-on).
    """
    n = max(1, int(n))
    H = homography_unit_square_to_quad(corners)
    m = Mesh()
    for j in range(n + 1):
        for i in range(n + 1):
            u, v = i / n, j / n
            x, y, w = H @ np.array([u, v, 1.0])
            m.verts.append([float(x / w), float(y / w), u, v])
    for j in range(n):
        for i in range(n):
            a = j * (n + 1) + i
            b, c, d = a + 1, a + n + 1, a + n + 2
            m.tris += [[a, b, d], [a, d, c]]
    return m


def regular_polygon_mesh(cx: float, cy: float, r: float, sides: int = 6) -> Mesh:
    """A fan of `sides` triangles around a centre vertex, media fitted to the bounding box."""
    m = Mesh()
    m.verts.append([cx, cy, 0.5, 0.5])
    for k in range(sides):
        a = 2 * np.pi * k / sides - np.pi / 2
        m.verts.append([cx + r * np.cos(a), cy + r * np.sin(a), 0.5 + 0.5 * np.cos(a), 0.5 + 0.5 * np.sin(a)])
    for k in range(sides):
        m.tris.append([0, 1 + k, 1 + (k + 1) % sides])
    return m


# -- primitives ---------------------------------------------------------------
def point_in_triangle(a, b, c, p) -> bool:
    def sign(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])

    d1, d2, d3 = sign(p, a, b), sign(p, b, c), sign(p, c, a)
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_neg and has_pos)


def point_segment_distance(p, a, b) -> float:
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 <= 1e-12:
        return float(np.hypot(p[0] - ax, p[1] - ay))
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / l2))
    return float(np.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy)))
