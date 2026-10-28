    return zmin, zmax

def estimate(specs, zmin=None, zmax=None, ts=256):
    """specs = [(w, h, gcps, method)]"""
    info = [prep(w, h, g, m)[2:] for w, h, g, m in specs]
    b, mpp = union(info); zn = native_z(mpp, ts); zmin, zmax = zrange(zn, zmin, zmax)
    counts = {z: tile_count(b, z) for z in range(zmin, zmax + 1)}
    return {"native": zn, "zmin": zmin, "zmax": zmax, "counts": counts, "total": sum(counts.values())}

# ---------- échantillonnage ----------
def bilinear(arr, p):
    h, w = arr.shape[:2]; u, v = p[:, 0] - .5, p[:, 1] - .5
    ok = (u >= 0) & (u <= w - 1) & (v >= 0) & (v <= h - 1)
    u, v = np.clip(u, 0, w - 1.001), np.clip(v, 0, h - 1.001)
    iu, iv = u.astype(int), v.astype(int); fu, fv = (u - iu)[:, None], (v - iv)[:, None]
    c = (arr[iv, iu] * (1 - fu) * (1 - fv) + arr[iv, iu + 1] * fu * (1 - fv) +
         arr[iv + 1, iu] * (1 - fu) * fv + arr[iv + 1, iu + 1] * fu * fv)
    return c, ok

def to_rgba(c, ok):
    a = c[:, 3] if c.shape[1] == 4 else np.full(len(c), 255.0)
    return np.concatenate([c[:, :3], np.where(ok, a, 0)[:, None]], axis=1).astype(np.uint8)

def composite(parts):
    """parts : liste de tableaux (n,4) uint8, du dessous vers le dessus."""
    if len(parts) == 1: return parts[0]
    acc, acc_a = 0.0, 0.0
    for p in parts:
        a = p[:, 3:4] / 255.0; acc = p[:, :3] * a + acc * (1 - a); acc_a = a + acc_a * (1 - a)
    return np.concatenate([acc / np.maximum(acc_a, 1e-6), acc_a * 255], axis=1).astype(np.uint8)

def _up(A, n, step, axis):
    t = np.arange(n) / step; i0 = np.minimum(t.astype(int), A.shape[axis] - 2); f = t - i0
    if axis == 1: return A[:, i0] * (1 - f) + A[:, i0 + 1] * f
    return A[i0, :] * (1 - f)[:, None] + A[i0 + 1, :] * f[:, None]

def uv_grid(inv, xs, ys, W, H, step):
    """Coordonnées pixel source pour une grille W×H, calculées sur une grille grossière (pas `step`) puis interpolées."""
    X, Y = np.meshgrid(xs, ys)
    P = inv(np.stack([X.ravel(), Y.ravel()], 1)).reshape(len(ys), len(xs), 2)
    return _up(_up(P[..., 0], W, step, 1), H, step, 0), _up(_up(P[..., 1], W, step, 1), H, step, 0)

def reduced(layer, scale):
    k = int(scale) if scale >= 2 else 1
    return (np.asarray(layer.im.reduce(k)), float(k)) if k > 1 else (np.asarray(layer.im), 1.0)

def make_tiles(layers, out_dir, progress=lambda f: None, zmin=None, zmax=None, ts=256):
    b, mpp = union([(l.b, l.mpp) for l in layers])
    zmin, zmax = zrange(native_z(mpp, ts), zmin, zmax)
    total = sum(tile_count(b, z) for z in range(zmin, zmax + 1))
    if total > MAX_TILES:
        raise ValueError(f"{total:,} tuiles : trop (max {MAX_TILES:,}). Réduisez le zoom maximal.")
    os.makedirs(out_dir, exist_ok=True)
    done = [0]; lock = threading.Lock(); step = ts // 32; K = ts // step + 2
    pool = ThreadPoolExecutor(min(8, os.cpu_count() or 2))
    for z in range(zmax, zmin - 1, -1):
        size = 2 * ORIGIN / 2 ** z; res = size / ts
        srcs = [reduced(l, res / l.mpp) for l in layers]
        x0, x1, y0, y1 = tile_range(b, z)
        def job(t):
            tx, ty = t; ox, oy = -ORIGIN + tx * size, ORIGIN - ty * size
            parts = []
            for l, (arr, k) in zip(layers, srcs):
                if l.b[2] < ox or l.b[0] > ox + size or l.b[3] < oy - size or l.b[1] > oy: continue
                xs = ox + (np.arange(K) * step + .5) * res; ys = oy - (np.arange(K) * step + .5) * res
                U, V = uv_grid(l.inv, xs, ys, ts, ts, step)
                c, ok = bilinear(arr, np.stack([U.ravel() / k, V.ravel() / k], 1))
                if ok.any(): parts.append(to_rgba(c, ok))
            if parts:
                d = os.path.join(out_dir, str(z), str(tx)); os.makedirs(d, exist_ok=True)
                Image.fromarray(composite(parts).reshape(ts, ts, 4), "RGBA").save(os.path.join(d, f"{ty}.png"), compress_level=1)
            with lock:
                done[0] += 1
                if done[0] % 25 == 0: progress(done[0] / total)
        jobs = [(tx, ty) for tx in range(x0, x1 + 1) for ty in range(y0, y1 + 1)]
        for i in range(0, len(jobs), 4000): list(pool.map(job, jobs[i:i + 4000]))
    pool.shutdown()
    west, south = m2ll(b[0], b[1]); east, north = m2ll(b[2], b[3])
    return [west, south, east, north], zmin, zmax

def render_raster(layers, crs="3857", max_px=8000):
    """Raster nord en haut (collage des calques). crs '3857' ou '4326'. max_px<=0 : résolution native.
    Retourne (RGBA, x0, y0, résolution)."""
    b, mpp = union([(l.b, l.mpp) for l in layers])
    if crs == "4326":
        west, south = m2ll(b[0], b[1]); east, north = m2ll(b[2], b[3]); span = max(east - west, north - south)
        res = mpp / 111320 if max_px <= 0 else max(mpp / 111320, span / max_px)
        x0, y0, W, H = west, north, math.ceil((east - west) / res), math.ceil((north - south) / res)
    else:
        span = max(b[2] - b[0], b[3] - b[1])
        res = mpp if max_px <= 0 else max(mpp, span / max_px)
        x0, y0, W, H = b[0], b[3], math.ceil((b[2] - b[0]) / res), math.ceil((b[3] - b[1]) / res)
    if W * H > MAX_RASTER_PX:
        raise ValueError(f"raster de {W}×{H} px ({W * H / 1e6:.0f} Mpx) trop grand : choisissez une taille maximale plus petite")
    res_m = res * 111320 if crs == "4326" else res
    srcs = [reduced(l, res_m / l.mpp) for l in layers]
    out = np.zeros((H, W, 4), np.uint8); step = 32; rows_chunk = max(16, min(256, 3_000_000 // W))
    xn = np.arange((W - 1) // step + 2) * step
    for r0 in range(0, H, rows_chunk):
        r1 = min(H, r0 + rows_chunk); yn = r0 + np.arange((r1 - r0 - 1) // step + 2) * step
        if crs == "4326":
            xs = (x0 + (xn + .5) * res) * ORIGIN / 180
            ys = np.log(np.tan((90 + (y0 - (yn + .5) * res)) * math.pi / 360)) * R
        else:
            xs, ys = x0 + (xn + .5) * res, y0 - (yn + .5) * res
        parts = []
        for l, (arr, k) in zip(layers, srcs):
            if l.b[2] < xs.min() or l.b[0] > xs.max() or l.b[3] < ys.min() or l.b[1] > ys.max(): continue
            U, V = uv_grid(l.inv, xs, ys, W, r1 - r0, step)
            c, ok = bilinear(arr, np.stack([U.ravel() / k, V.ravel() / k], 1))
            parts.append(to_rgba(c, ok))
        if parts: out[r0:r1] = composite(parts).reshape(r1 - r0, W, 4)
    return out, x0, y0, res

def footprint(w, h, gcps, method, n=80):
    fwd = prep(w, h, gcps, method)[0]
    ring = [list(m2ll(x, y)) for x, y in fwd(edge_points(w, h, n))]
    return ring + [ring[0]]

def to_mbtiles(tiles_dir, path, bounds, zmin, zmax, name, ts=256):
    if os.path.exists(path): os.remove(path)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE metadata (name text, value text)")
    db.execute("CREATE TABLE tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob)")
    db.execute("CREATE UNIQUE INDEX t ON tiles (zoom_level, tile_column, tile_row)")
    meta = {"name": name, "format": "png", "type": "overlay", "version": "1", "tilesize": ts,
            "bounds": ",".join(f"{v:.6f}" for v in bounds), "minzoom": zmin, "maxzoom": zmax}
    db.executemany("INSERT INTO metadata VALUES (?,?)", [(k, str(v)) for k, v in meta.items()])
