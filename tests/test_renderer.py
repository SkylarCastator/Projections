"""Offscreen GPU tests. Skipped when no GL context can be created (CI without a display)."""
import numpy as np
import pytest

moderngl = pytest.importorskip("moderngl")

from promap.model.project import Surface
from promap.render.renderer import Renderer, letterbox
from promap.show.engine import RenderItem


@pytest.fixture(scope="module")
def ctx():
    try:
        c = moderngl.create_standalone_context(require=330)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"no GL: {exc}")
    yield c
    c.release()


def render(ctx, items, size=(200, 100), outlines=None):
    fbo = ctx.simple_framebuffer(size, components=4)
    r = Renderer(ctx)
    r.render(fbo, size, items, (0, 0, *size), outlines=outlines)
    data = np.frombuffer(fbo.read(components=3), dtype=np.uint8).reshape(size[1], size[0], 3)
    img = data[::-1].copy()  # GL rows are bottom-up; flip to image convention (row 0 = top)
    r.release()
    fbo.release()
    return img


def quadrant_frame():
    """8x8 frame: TL red, TR green, BL blue, BR white."""
    f = np.zeros((8, 8, 3), np.uint8)
    f[:4, :4] = (255, 0, 0)
    f[:4, 4:] = (0, 255, 0)
    f[4:, :4] = (0, 0, 255)
    f[4:, 4:] = (255, 255, 255)
    return f


def test_letterbox():
    assert letterbox(1920, 1080, 1920, 1080) == (0, 0, 1920, 1080)
    assert letterbox(1920, 1080, 960, 1000) == (0, 230, 960, 540)


def test_orientation_and_coverage(ctx):
    s = Surface(corners=[[20, 10], [180, 10], [180, 90], [20, 90]])
    img = render(ctx, [RenderItem("a", s, quadrant_frame(), 0, 1.0)])
    assert tuple(img[15, 25]) == (255, 0, 0), "image top-left should land on the TL corner"
    assert tuple(img[15, 175]) == (0, 255, 0)
    assert tuple(img[85, 25]) == (0, 0, 255)
    assert tuple(img[85, 175]) == (255, 255, 255)
    assert tuple(img[5, 5]) == (0, 0, 0), "outside the quad stays black"


def test_keystone_warp_is_projective(ctx):
    # trapezoid: top edge narrower than bottom. The image centre must land on the
    # quad's projective centre (intersection of the diagonals), not the centroid.
    corners = [[80, 10], [120, 10], [190, 90], [10, 90]]
    s = Surface(corners=corners)
    f = np.zeros((64, 64, 3), np.uint8)
    f[:, :] = (0, 0, 255)
    f[30:34, :] = (255, 0, 0)  # horizontal red line through the image centre
    img = render(ctx, [RenderItem("a", s, f, 0, 1.0)])
    # diagonals of the trapezoid intersect at y = 10 + 80 * t where t solves the
    # projective midline; for this symmetric case y ~ 10 + 80 * (40/(40+180)) ... just
    # assert the red line sits ABOVE the centroid row (50): perspective pulls it up.
    red_rows = np.where((img[:, 100] == (255, 0, 0)).all(axis=1))[0]
    assert len(red_rows) > 0
    assert red_rows.mean() < 48


def test_mask_and_opacity(ctx):
    base = Surface(corners=[[0, 0], [200, 0], [200, 100], [0, 100]])
    mask = Surface(kind="mask", corners=[[100, 0], [200, 0], [200, 100], [100, 100]])
    white = np.full((4, 4, 3), 255, np.uint8)
    img = render(ctx, [RenderItem("a", base, white, 0, 0.5), RenderItem("m", mask, None, 0, 1.0)])
    assert 120 <= img[50, 50, 0] <= 135, "50% opacity over black = mid grey"
    assert tuple(img[50, 150]) == (0, 0, 0), "mask paints black on top"


def test_flip_flags(ctx):
    s = Surface(corners=[[20, 10], [180, 10], [180, 90], [20, 90]], flip_h=True, flip_v=True)
    img = render(ctx, [RenderItem("a", s, quadrant_frame(), 0, 1.0)])
    assert tuple(img[15, 25]) == (255, 255, 255)
    assert tuple(img[85, 175]) == (255, 0, 0)


def test_mesh_two_triangles_cover_square_with_uv(ctx):
    from promap.model.mesh import quad_grid

    s = Surface(mesh=quad_grid([[20, 10], [180, 10], [180, 90], [20, 90]], 1))
    img = render(ctx, [RenderItem("a", s, quadrant_frame(), 0, 1.0)])
    assert tuple(img[15, 25]) == (255, 0, 0)
    assert tuple(img[15, 175]) == (0, 255, 0)
    assert tuple(img[85, 25]) == (0, 0, 255)
    assert tuple(img[85, 175]) == (255, 255, 255)
    assert tuple(img[5, 5]) == (0, 0, 0)


def test_mesh_single_triangle_leaves_other_half_dark(ctx):
    from promap.model.mesh import triangle_mesh

    s = Surface(mesh=triangle_mesh([[0, 0], [200, 0], [0, 100]]))
    white = np.full((4, 4, 3), 255, np.uint8)
    img = render(ctx, [RenderItem("a", s, white, 0, 1.0)])
    assert tuple(img[10, 10]) == (255, 255, 255)
    assert tuple(img[90, 190]) == (0, 0, 0)


def test_subdivided_quad_matches_projective_quad(ctx):
    from promap.model.mesh import quad_grid

    corners = [[80, 10], [120, 10], [190, 90], [10, 90]]
    f = np.zeros((64, 64, 3), np.uint8)
    f[:, :] = (0, 0, 255)
    f[30:34, :] = (255, 0, 0)
    ref = render(ctx, [RenderItem("a", Surface(corners=corners), f, 0, 1.0)])
    sub = render(ctx, [RenderItem("a", Surface(mesh=quad_grid(corners, 8)), f, 0, 1.0)])
    r1 = np.where((ref[:, 100] == (255, 0, 0)).all(axis=1))[0].mean()
    r2 = np.where((sub[:, 100] == (255, 0, 0)).all(axis=1))[0].mean()
    assert abs(r1 - r2) <= 1.5, "8x8 grid reproduces the perspective warp to a pixel"


def test_tint(ctx):
    s = Surface(corners=[[0, 0], [200, 0], [200, 100], [0, 100]], tint=[1.0, 0.0, 0.5])
    white = np.full((4, 4, 3), 255, np.uint8)
    img = render(ctx, [RenderItem("a", s, white, 0, 1.0)])
    r, g, b = img[50, 100]
    assert r == 255 and g == 0 and 120 <= b <= 135


def test_luma_key_makes_black_transparent(ctx):
    base = Surface(corners=[[0, 0], [200, 0], [200, 100], [0, 100]])
    top = Surface(corners=[[0, 0], [200, 0], [200, 100], [0, 100]], key_black=True, key_threshold=0.12)
    blue = np.zeros((4, 4, 3), np.uint8)
    blue[:, :] = (0, 0, 255)
    f = np.zeros((8, 8, 3), np.uint8)
    f[:, 4:] = (255, 255, 255)  # right half white, left half black
    img = render(ctx, [RenderItem("a", base, blue, 0, 1.0), RenderItem("b", top, f, 0, 1.0)])
    assert tuple(img[50, 50]) == (0, 0, 255), "black half keyed out: blue shows through"
    assert tuple(img[50, 150]) == (255, 255, 255), "white half opaque"
    top.key_black = False
    img = render(ctx, [RenderItem("a", base, blue, 0, 1.0), RenderItem("b", top, f, 0, 1.0)])
    assert tuple(img[50, 50]) == (0, 0, 0), "key off: black paints over"
