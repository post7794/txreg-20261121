"""CLIP 常驻服务：模型加载一次，POST JSON {"text":..,"image":path,"regions":[[x0,y0,x1,y1]*6]} -> {"picks":[...],"scores":[...]}"""
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.environ.setdefault("TX_CLIP_MODEL", str(_HERE / "clip-model"))
import numpy as np
from clip_guess import _load, to_english, score_cells
_load()  # load model once at startup
LOCK = threading.Lock()

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        try:
            from PIL import Image
            im = Image.open(req["image"]).convert("RGB")
            regions = req.get("regions")
            if regions:
                from clip_guess import split_cells_by_regions
                cells = split_cells_by_regions(im, regions)
            else:
                from clip_guess import split_image_grid
                cells = split_image_grid(im)
            with LOCK:
                ranked = score_cells(req["text"], cells)
            out = {"text": req["text"], "english": to_english(req["text"]),
                   "picks": [], "scores": [{"cell": r["index"]+1, "score": round(r["score"],4)} for r in ranked]}
            best = ranked[0]
            if best["score"] >= 0.185 and (len(ranked) < 2 or ranked[1]["score"] <= best["score"] - 0.015):
                out["picks"] = [best["index"] + 1]
            body = json.dumps(out, ensure_ascii=False).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            body = json.dumps({"error": str(e)}).encode()
            self.send_response(500); self.end_headers(); self.wfile.write(body)

HTTPServer(("127.0.0.1", 8701), H).serve_forever()
