#!/usr/bin/env python
"""clip_similarity.py - OPTIONAL helper for bs_miro.py `cluster`. Imported only when clustering,
so the core push/resync/verify never need torch. Provides CLIP ViT-B/32 image embeddings and a
chronological-walk column grouping (consecutive visually-similar shots share a column).

Needs: numpy, torch, transformers, pillow. Frames are fetched from public thumbnail URLs.
"""
import urllib.request
from io import BytesIO


def _load_model():
    import torch
    from transformers import CLIPModel, CLIPProcessor
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32").to(device).eval()
    proc = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
    return model, proc, device


def _fetch(url):
    from PIL import Image
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = urllib.request.urlopen(req, timeout=30).read()
    return Image.open(BytesIO(data)).convert("RGB")


def embed_urls(urls, batch=64):
    """Return a numpy array (N, d) of unit-norm CLIP image embeddings, one per URL. Rows for
    URLs that fail to fetch are zero vectors (cosine 0 -> naturally start a new column)."""
    import numpy as np
    import torch
    model, proc, device = _load_model()
    embs = [None] * len(urls)
    idx, imgs = [], []

    def flush():
        if not imgs:
            return
        with torch.no_grad():
            inp = proc(images=imgs, return_tensors="pt").to(device)
            vis = model.vision_model(pixel_values=inp["pixel_values"])
            f = model.visual_projection(vis.pooler_output)
            f = torch.nn.functional.normalize(f, dim=-1).cpu().numpy()
        for j, e in zip(idx, f):
            embs[j] = e
        idx.clear()
        imgs.clear()

    for i, u in enumerate(urls):
        try:
            imgs.append(_fetch(u))
            idx.append(i)
        except Exception:
            pass
        if len(imgs) >= batch:
            flush()
    flush()
    d = next((e.shape[0] for e in embs if e is not None), 512)
    return np.vstack([e if e is not None else np.zeros(d, dtype="float32") for e in embs])


def walk_columns(shots, embs, threshold=0.75):
    """Chronological walk: shots is TC-ordered; group consecutive shots into columns while each
    stays >= threshold cosine to the running column centroid. Returns list[list[shot]]."""
    import numpy as np
    columns = []
    cur, cur_vecs = [], []
    for s, v in zip(shots, embs):
        if not cur:
            cur, cur_vecs = [s], [v]
            continue
        centroid = np.mean(cur_vecs, axis=0)
        nc = np.linalg.norm(centroid) or 1.0
        nv = np.linalg.norm(v) or 1.0
        sim = float(np.dot(centroid, v) / (nc * nv))
        if sim >= threshold:
            cur.append(s)
            cur_vecs.append(v)
        else:
            columns.append(cur)
            cur, cur_vecs = [s], [v]
    if cur:
        columns.append(cur)
    return columns
