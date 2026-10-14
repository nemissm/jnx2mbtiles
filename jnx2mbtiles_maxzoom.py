#!/usr/bin/env python3
"""Convert Garmin JNX to .mbtiles keeping ONLY the maximum-zoom level (size reduction)."""
import os
import sys
import math
import struct
import sqlite3


def read_str(f):
    buf = bytearray()
    while True:
        b = f.read(1)
        if not b or b == b"\x00":
            break
        buf += b
    return bytes(buf)


def parse_jnx(path):
    with open(path, "rb") as fh:
        data = fh.read()
    f = __import__("io").BytesIO(data)

    version = struct.unpack("<I", f.read(4))[0]
    f.read(4)  # devid
    lat1 = struct.unpack("<i", f.read(4))[0]
    lon1 = struct.unpack("<i", f.read(4))[0]
    lat2 = struct.unpack("<i", f.read(4))[0]
    lon2 = struct.unpack("<i", f.read(4))[0]
    details = struct.unpack("<I", f.read(4))[0]
    f.read(24)  # expire, product_id, crc, signature, sig_offset, +1 reserved

    levels_meta = []
    for i in range(details):
        num_tiles = struct.unpack("<I", f.read(4))[0]
        offset = struct.unpack("<I", f.read(4))[0]
        scale = struct.unpack("<I", f.read(4))[0]
        if version == 4:
            f.read(4)          # unknown
            read_str(f)        # copyright
        levels_meta.append({"num_tiles": num_tiles, "offset": offset, "scale": scale})

    f.read(4)  # block_version
    group_id = read_str(f)
    read_str(f)      # group_name
    read_str(f)      # unknown_str
    f.read(2)        # product_id short
    map_name = read_str(f)
    f.read(4)        # details2

    level_names = []
    for i in range(details):
        name = read_str(f)
        read_str(f)   # desc
        read_str(f)   # copyright
        zoom = struct.unpack("<I", f.read(4))[0]
        level_names.append({"name": name, "zoom": zoom})

    TILE_FMT = "<IIIIhhII"
    TILE_SIZE = struct.calcsize(TILE_FMT)
    tiles = []
    for li, meta in enumerate(levels_meta):
        f.seek(meta["offset"])
        for j in range(meta["num_tiles"]):
            raw = f.read(TILE_SIZE)
            if len(raw) < TILE_SIZE:
                break
            top, right, bottom, left, w, h, size, off = struct.unpack(TILE_FMT, raw)
            tiles.append({"level": li, "idx": j, "top": top, "right": right,
                          "bottom": bottom, "left": left, "w": w, "h": h,
                          "size": size, "offset": off,
                          "zoom": level_names[li]["zoom"]})

    return {"version": version, "data": data, "details": details,
            "lat1": lat1, "lon1": lon1, "lat2": lat2, "lon2": lon2,
            "group_id": group_id, "map_name": map_name,
            "levels_meta": levels_meta, "level_names": level_names, "tiles": tiles}


def grad_to_deg(v):
    return v * 180.0 / 0x7FFFFFFF


def deg2tile(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def mercator(lon, lat):
    R = 6378137.0
    x = R * math.radians(lon)
    lat_r = math.radians(lat)
    y = R * math.log(math.tan(math.pi / 4 + lat_r / 2))
    return x, y


def main():
    if len(sys.argv) < 2:
        print("usage: jnx2mbtiles_maxzoom.py <file.jnx> [out.mbtiles]")
        sys.exit(1)
    path = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(path)[0] + "_maxzoom.mbtiles"

    info = parse_jnx(path)
    data = info["data"]

    # keep only the highest zoom level
    max_zoom = max(ln["zoom"] for ln in info["level_names"])
    kept_levels = {li for li, ln in enumerate(info["level_names"]) if ln["zoom"] == max_zoom}

    print("JNX:", path)
    print("  map name:", repr(info["map_name"]))
    print("  all zooms:", [ln["zoom"] for ln in info["level_names"]])
    print("  keeping only max zoom: z%d" % max_zoom)

    if os.path.exists(out):
        os.remove(out)
    con = sqlite3.connect(out)
    cur = con.cursor()
    cur.executescript("""
CREATE TABLE metadata (name TEXT, value TEXT);
CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);
""")

    inserted = skipped = 0
    buf = []
    for t in info["tiles"]:
        if t["level"] not in kept_levels:
            continue
        if t["size"] == 0:
            skipped += 1
            continue
        jpeg = data[t["offset"]:t["offset"] + t["size"]]
        if not jpeg.startswith(b"\xff\xd8"):
            jpeg = b"\xff\xd8" + jpeg
        z = t["zoom"]
        lat_c = (grad_to_deg(t["top"]) + grad_to_deg(t["bottom"])) / 2.0
        lon_c = (grad_to_deg(t["left"]) + grad_to_deg(t["right"])) / 2.0
        if not (-85.051129 < lat_c < 85.051129 and -180.0 <= lon_c <= 180.0):
            skipped += 1
            continue
        tx, ty = deg2tile(lat_c, lon_c, z)
        tms_y = (2 ** z) - 1 - ty
        buf.append((z, tx, tms_y, sqlite3.Binary(jpeg)))
        if len(buf) >= 5000:
            cur.executemany("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)", buf)
            inserted += len(buf); buf.clear(); con.commit()
    if buf:
        cur.executemany("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)", buf)
        inserted += len(buf); con.commit()

    top, right = grad_to_deg(info["lat1"]), grad_to_deg(info["lon1"])
    bottom, left = grad_to_deg(info["lat2"]), grad_to_deg(info["lon2"])
    xmin, ymax = mercator(left, top)
    xmax, ymin = mercator(right, bottom)
    cx, cy = (left + right) / 2, (top + bottom) / 2
    meta = [
        ("name", info["map_name"].decode("utf-8", "replace") if isinstance(info["map_name"], bytes) else info["map_name"]),
        ("format", "jpeg"),
        ("minzoom", str(max_zoom)),
        ("maxzoom", str(max_zoom)),
        ("bounds", "%.6f,%.6f,%.6f,%.6f" % (xmin, ymin, xmax, ymax)),
        ("center", "%.6f,%.6f,%d" % (cy, cx, max_zoom)),
        ("type", "basemap"),
        ("version", "2"),
    ]
    cur.executemany("INSERT OR REPLACE INTO metadata VALUES (?,?)", meta)
    con.commit()
    con.close()

    size = os.path.getsize(out)
    src_size = os.path.getsize(path)
    print("\nWrote %s (%.2f MB)" % (out, size / 1e6))
    print("  source jnx: %.2f MB" % (src_size / 1e6))
    print("  tiles: %d (skipped %d)  zoom: z%d only" % (inserted, skipped, max_zoom))


if __name__ == "__main__":
    main()
