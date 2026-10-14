"""! @file
@brief Running an upscaling net on a picture: tiles, padding, alpha and precision.

`Upscaler(net, scale, device, half)` is the callable handle the `upscale`
capability serves: `run(rgb_uint8, tile=512, progress=None)` -> the picture
`scale` times larger, same channel count. Big pictures go through in
overlapping tiles so memory stays bounded; a tile that runs out of GPU memory
is retried at half the size. Alpha is resized with Lanczos (nets see RGB only);
gray pictures run as RGB and come back gray.
"""
import numpy as np
from PIL import Image

from optional_deps import optional_import

from . import esrgan_arch

torch, HAVE_TORCH = optional_import("torch")
spandrel, HAVE_SPANDREL = optional_import("spandrel")

TILE_PAD = 24
MIN_TILE = 64


def load_model(path, device, half=False):
    """! @brief An Upscaler for a checkpoint: the built-in ESRGAN nets, else spandrel.
    @throws esrgan_arch.UnsupportedModel / RuntimeError with the reason.
    """
    sd = esrgan_arch.read_state_dict(path)
    try:
        net, arch = esrgan_arch.build(sd)
        scale = net.scale
    except esrgan_arch.UnsupportedModel as e:
        if not HAVE_SPANDREL:
            raise esrgan_arch.UnsupportedModel(
                "%s (install spandrel for other architectures)" % e) from None
        desc = spandrel.ModelLoader().load_from_state_dict(sd)
        if not isinstance(desc, spandrel.ImageModelDescriptor):
            raise esrgan_arch.UnsupportedModel("%s is not an image-to-image model" % path) from None
        net, arch, scale = desc.model, getattr(desc.architecture, "name", "spandrel"), desc.scale
        if half and not getattr(desc, "supports_half", True):
            half = False
    net.eval()
    for p in net.parameters():
        p.requires_grad_(False)
    dev = torch.device(device)
    half = bool(half and dev.type == "cuda")
    net = net.to(dev)
    if half:
        net = net.half()
    return Upscaler(net, scale, dev, half, arch=arch, path=path)


class Upscaler:
    """! @brief A loaded net plus how to feed it."""

    def __init__(self, net, scale, device, half, arch="", path=""):
        self.net = net
        self.scale = scale
        self.device = device
        self.half = half
        self.arch = arch
        self.model_path = path

    def __call__(self, img, tile=512, progress=None):
        return self.run(img, tile=tile, progress=progress)

    # -- whole picture ----------------------------------------------------
    def run(self, img, tile=512, progress=None):
        """! @brief Upscale an HxW / HxWx3 / HxWx4 uint8 picture by the net's scale."""
        img = np.asarray(img)
        if img.dtype != np.uint8:
            raise ValueError("expected a uint8 picture")
        gray = img.ndim == 2 or img.shape[2] == 1
        alpha = None
        if gray:
            rgb = np.repeat(img.reshape(img.shape[0], img.shape[1], 1), 3, axis=2)
        elif img.shape[2] == 4:
            rgb, alpha = img[:, :, :3], img[:, :, 3]
        else:
            rgb = img[:, :, :3]
        out = self._run_rgb(np.ascontiguousarray(rgb), int(tile or 0), progress)
        oh, ow = out.shape[:2]
        if gray:
            return np.asarray(Image.fromarray(out).convert("L"))
        if alpha is not None:
            a = np.asarray(Image.fromarray(alpha).resize((ow, oh), Image.LANCZOS))
            out = np.dstack([out, a])
        return out

    def _to_tensor(self, arr):
        t = torch.from_numpy(np.array(arr)).permute(2, 0, 1).unsqueeze(0).to(self.device)
        t = t.half() if self.half else t.float()
        return t / 255.0

    @staticmethod
    def _to_array(t):
        t = t.squeeze(0).float().clamp_(0, 1).mul_(255.0).round_()
        return t.byte().permute(1, 2, 0).cpu().numpy()

    def _run_rgb(self, rgb, tile, progress):
        h, w = rgb.shape[:2]
        while True:
            try:
                with torch.inference_mode():
                    if tile <= 0 or (h <= tile and w <= tile):
                        out = self._infer(rgb)
                        if progress:
                            progress(1, 1)
                        return out
                    return self._tiled(rgb, tile, progress)
            except torch.cuda.OutOfMemoryError:
                torch.cuda.empty_cache()
                tile = (tile or max(h, w)) // 2
                if tile < MIN_TILE:
                    raise RuntimeError("out of GPU memory even with small tiles")

    def _infer(self, rgb):
        """! @brief One pass; the input is edge-padded to what the net needs (unshuffle)."""
        h, w = rgb.shape[:2]
        mult = getattr(self.net, "unshuffle", 1) or 1
        ph, pw = (-h) % mult, (-w) % mult
        if ph or pw:
            rgb = np.pad(rgb, ((0, ph), (0, pw), (0, 0)), mode="edge")
        out = self._to_array(self.net(self._to_tensor(rgb)))
        s = self.scale
        return out[: int(round(h * s)), : int(round(w * s))]

    def _tiled(self, rgb, tile, progress):
        """! @brief Overlapping tiles; each keeps only its own (unpadded) part of the output."""
        h, w = rgb.shape[:2]
        s = self.scale
        out = np.zeros((int(round(h * s)), int(round(w * s)), 3), np.uint8)
        ys = list(range(0, h, tile))
        xs = list(range(0, w, tile))
        total, done = len(ys) * len(xs), 0
        for y in ys:
            for x in xs:
                y0, y1 = max(0, y - TILE_PAD), min(h, y + tile + TILE_PAD)
                x0, x1 = max(0, x - TILE_PAD), min(w, x + tile + TILE_PAD)
                part = self._infer(rgb[y0:y1, x0:x1])
                ty, tx = int(round((y - y0) * s)), int(round((x - x0) * s))
                th = int(round(min(tile, h - y) * s))
                tw = int(round(min(tile, w - x) * s))
                oy, ox = int(round(y * s)), int(round(x * s))
                th, tw = min(th, out.shape[0] - oy), min(tw, out.shape[1] - ox)
                out[oy:oy + th, ox:ox + tw] = part[ty:ty + th, tx:tx + tw]
                done += 1
                if progress:
                    progress(done, total)
        return out


def resize_to(img, width, height):
    """! @brief Lanczos resize (to reach a factor other than the net's own)."""
    if img.shape[1] == width and img.shape[0] == height:
        return img
    return np.asarray(Image.fromarray(img).resize((int(width), int(height)), Image.LANCZOS))