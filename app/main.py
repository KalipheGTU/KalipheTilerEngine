@app.get("/api/maps/{mid}")
def status(mid: str): return meta(mdir(mid))

@app.post("/api/maps/{mid}/export")
def export(mid: str, req: ExportReq):
    d = mdir(mid); m = meta(d)
    extra = {"original", "current", "limits"}
    kinds = [k for k in req.kinds if k in exports.KINDS or k in extra]
    if not kinds: raise HTTPException(400, "aucun format choisi")
    layers = build_layers(req.layers) if any(k in exports.KINDS for k in kinds) else []
    try:
        files = exports.export_all(d, m["name"], layers, [k for k in kinds if k in exports.KINDS], req.crs, req.max_px, m) if any(k in exports.KINDS for k in kinds) else []
        out = d / "exports"; out.mkdir(exist_ok=True)
        # Exports sans perte de l'original et de l'image actuellement modifiée/rognée.
        if "original" in kinds:
            src = d / ("orig.jpg" if (d / "orig.jpg").exists() else "orig.png")
            target = out / (m.get("name", "image") + "_original" + src.suffix)
            shutil.copy2(src, target); files.append(target.name)
        if "current" in kinds:
            src = imageops.src_path(d)
            target = out / (m.get("name", "image") + "_courante" + src.suffix)
            shutil.copy2(src, target); files.append(target.name)
        if "limits" in kinds:
            layers_meta=[]
            for l in req.layers:
                mm=meta(mdir(l.id)); ring=georef.footprint(mm["width"], mm["height"], [g.model_dump() for g in l.gcps], l.method) if len(l.gcps) >= georef.MIN_PTS[l.method] else []
                layers_meta.append({"id":l.id,"name":l.name or mm.get("name","image"),"image_pixels":{"width":mm["width"],"height":mm["height"],"pixel_bounds":[0,0,mm["width"],mm["height"]]},"footprint_lon_lat":ring,"gcps":[g.model_dump() for g in l.gcps]})
            target=out/(m.get("name","image")+"_limites.json")
            target.write_text(json.dumps({"crs":"EPSG:4326","layers":layers_meta},ensure_ascii=False,indent=2),encoding="utf-8");files.append(target.name)
    except Exception as e: raise HTTPException(400, str(e))
    return {"files": files, "folder": str(d / "exports")}

def do_upload(d, mid, req):
    try:
        save(d, upload={"status": "running", "progress": 0})
        r = exports.s3_upload(d, req.s3, req.what, mid, meta(d), lambda f: save(d, upload={"status": "running", "progress": round(f, 3)}))
        save(d, upload={"status": "done", **r})
    except Exception as e:
        save(d, upload={"status": "error", "error": str(e)})

@app.post("/api/maps/{mid}/upload")
def upload_cloud(mid: str, req: UploadReq):
    d = mdir(mid)
    for k in ("bucket", "access_key", "secret_key"):
        if not req.s3.get(k): raise HTTPException(400, f"champ manquant : {k}")
    threading.Thread(target=do_upload, args=(d, mid, req), daemon=True).start()
    return {"status": "running"}

IMGS = ("orig.jpg", "orig.png", "source.jpg", "source.png")
@app.post("/api/save_project")
def save_project(req: SaveReq):
    first = mdir(req.layers[0].id); out = DATA / f"{meta(first)['name']}.ktproj"
    proj = {"version": 2, **req.model_dump(exclude={"layers"}), "layers": [l.model_dump() for l in req.layers]}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        z.writestr("project.json", json.dumps(proj))
        for i, l in enumerate(req.layers):
            d = mdir(l.id)
            for f in IMGS:
                if (d / f).exists(): z.write(d / f, f"L{i}/{f}")
    return {"path": str(out)}

@app.post("/api/projects/open")
async def open_project(file: UploadFile = File(...)):
    made = []
    try:
        with zipfile.ZipFile(file.file) as z:
            proj = json.loads(z.read("project.json")); out = []
            for i, l in enumerate(proj.get("layers", [])):
                mid = new_id(); d = DATA / mid; d.mkdir(); made.append(d)
                for f in IMGS:
                    if f"L{i}/{f}" in z.namelist(): (d / f).write_bytes(z.read(f"L{i}/{f}"))
                save(d, status="uploaded", name=safe(l.get("name") or f"image{i + 1}"))
                out.append({**img_info(d, mid), "name": l.get("name") or meta(d)["name"], "gcps": l.get("gcps", []),
                            "method": l.get("method", "affine"), "vis": l.get("vis", True)})
        proj.pop("layers", None)
        return {"layers": out, "project": proj}
    except Exception as e:
        for d in made: shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, f"projet invalide : {e}")

@app.post("/api/maps/{mid}/open")
def open_folder(mid: str):
    d = mdir(mid)
    if sys.platform == "win32": os.startfile(d)
    return {"ok": True}

@app.post("/api/open_root")
def open_root():
    if sys.platform == "win32": os.startfile(DATA)
    return {"ok": True}

def load_settings():
    return json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}

@app.get("/api/settings")
def get_settings(): return load_settings()

@app.post("/api/settings")
def set_settings(s: dict):
    keep = {"mapbox": str(s.get("mapbox", "")), "google": str(s.get("google", "")),
            "s3": {k: str(v) for k, v in (s.get("s3") or {}).items()}}
    SETTINGS.write_text(json.dumps(keep)); return {"ok": True}

@app.get("/tiles/{prov}/{z}/{x}/{y}")
def tiles(prov: str, z: int, x: int, y: int):
    if not (0 <= z <= 24 and 0 <= x < 2 ** z and 0 <= y < 2 ** z): raise HTTPException(404)
    try:
        ct, body = basemaps.get_tile(prov, z, x, y, load_settings(), CACHE)
    except basemaps.TileError as e:
        return JSONResponse({"detail": e.msg}, status_code=e.status if e.status in (401, 403, 404) else 502)
    return Response(body, media_type=ct, headers={"Cache-Control": "max-age=86400"})

@app.get("/fonts/{stack}/{rng}.pbf")
def fonts(stack: str, rng: str):
    if not re.fullmatch(r"[A-Za-z0-9 ,_-]+", stack) or not re.fullmatch(r"\d+-\d+", rng): raise HTTPException(404)
    try: return Response(basemaps.get_font(stack, rng, CACHE), media_type="application/x-protobuf")
    except basemaps.TileError: raise HTTPException(404)

@app.get("/api/diag")
def diag():
    return {"data_dir": str(DATA), "frozen": bool(getattr(sys, "frozen", False)), "tests": basemaps.diag(load_settings(), CACHE)}

@app.get("/", response_class=HTMLResponse)
def home(): return (STATIC / "index.html").read_text(encoding="utf-8")
