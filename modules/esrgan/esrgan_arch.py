"""! @file
@brief ESRGAN-family networks and the checkpoint reader that builds the right one.

Two architectures, every layout of their checkpoints:

  RRDBNet          ESRGAN (the original "model.N" keys, 1x / 2x / 4x / 8x / 16x
                   by its number of upsample convs) and BasicSR / Real-ESRGAN
                   ("conv_first", "body.N.rdbN" keys; x2 / x1 models feed a
                   pixel-unshuffled input, read off conv_first's input width);
                   any block count, feature width and growth channels.
  SRVGGNetCompact  Real-ESRGAN's compact nets (realesr-general-x4v3,
                   realesr-animevideov3): "body.N" convs and PReLUs ending in a
                   pixel shuffle, plus the nearest-upsampled input.

Checkpoints are .pth / .pt / .ckpt (torch.load, weights only) or .safetensors,
bare or wrapped in params_ema / params / state_dict / model, with or without a
"module." prefix. Architectures outside these two are left to spandrel when it
is installed (see upscaler.load_model).
"""
import math
import re

from optional_deps import optional_import

torch, HAVE_TORCH = optional_import("torch")
safetensors_torch, HAVE_SAFETENSORS = optional_import("safetensors.torch")
if HAVE_TORCH:
    from torch import nn
    from torch.nn import functional as F

_WRAPPERS = ("params_ema", "params", "state_dict", "model", "net_g")


class UnsupportedModel(ValueError):
    """! @brief The checkpoint is not an architecture this file builds."""


def read_state_dict(path):
    """! @brief A checkpoint's tensors as a flat {key: tensor} dict on the CPU."""
    if path.lower().endswith(".safetensors"):
        if not HAVE_SAFETENSORS:
            raise RuntimeError("the safetensors package is needed to read %s" % path)
        sd = safetensors_torch.load_file(path, device="cpu")
    else:
        sd = torch.load(path, map_location="cpu", weights_only=True)
    for _ in range(3):
        if not isinstance(sd, dict):
            break
        inner = next((sd[k] for k in _WRAPPERS if isinstance(sd.get(k), dict)), None)
        if inner is None or any(torch.is_tensor(v) for v in sd.values()):
            break
        sd = inner
    if not isinstance(sd, dict) or not sd:
        raise UnsupportedModel("no weights in the checkpoint")
    out = {}
    for k, v in sd.items():
        if torch.is_tensor(v):
            out[k[7:] if k.startswith("module.") else k] = v
    return out


if HAVE_TORCH:
    class ResidualDenseBlock(nn.Module):
        """! @brief Five densely connected convs with a scaled residual."""

        def __init__(self, nf=64, gc=32):
            super().__init__()
            self.conv1 = nn.Conv2d(nf, gc, 3, 1, 1)
            self.conv2 = nn.Conv2d(nf + gc, gc, 3, 1, 1)
            self.conv3 = nn.Conv2d(nf + 2 * gc, gc, 3, 1, 1)
            self.conv4 = nn.Conv2d(nf + 3 * gc, gc, 3, 1, 1)
            self.conv5 = nn.Conv2d(nf + 4 * gc, nf, 3, 1, 1)
            self.lrelu = nn.LeakyReLU(0.2, inplace=True)

        def forward(self, x):
            x1 = self.lrelu(self.conv1(x))
            x2 = self.lrelu(self.conv2(torch.cat((x, x1), 1)))
            x3 = self.lrelu(self.conv3(torch.cat((x, x1, x2), 1)))
            x4 = self.lrelu(self.conv4(torch.cat((x, x1, x2, x3), 1)))
            x5 = self.conv5(torch.cat((x, x1, x2, x3, x4), 1))
            return x5 * 0.2 + x

    class RRDB(nn.Module):
        """! @brief Residual-in-residual dense block."""

        def __init__(self, nf, gc=32):
            super().__init__()
            self.rdb1 = ResidualDenseBlock(nf, gc)
            self.rdb2 = ResidualDenseBlock(nf, gc)
            self.rdb3 = ResidualDenseBlock(nf, gc)

        def forward(self, x):
            out = self.rdb3(self.rdb2(self.rdb1(x)))
            return out * 0.2 + x

    class RRDBNet(nn.Module):
        """! @brief ESRGAN / Real-ESRGAN generator.
        @param n_up     nearest-x2 upsample convs (2 -> x4 before unshuffle).
        @param unshuffle  pixel-unshuffle factor of the input (Real-ESRGAN x2: 2, x1: 4).
        """

        def __init__(self, in_nc=3, out_nc=3, nf=64, nb=23, gc=32, n_up=2, unshuffle=1):
            super().__init__()
            self.unshuffle = unshuffle
            self.n_up = n_up
            self.in_nc = in_nc
            self.conv_first = nn.Conv2d(in_nc * unshuffle * unshuffle, nf, 3, 1, 1)
            self.body = nn.Sequential(*[RRDB(nf, gc) for _ in range(nb)])
            self.conv_body = nn.Conv2d(nf, nf, 3, 1, 1)
            for i in range(n_up):
                setattr(self, "conv_up%d" % (i + 1), nn.Conv2d(nf, nf, 3, 1, 1))
            self.conv_hr = nn.Conv2d(nf, nf, 3, 1, 1)
            self.conv_last = nn.Conv2d(nf, out_nc, 3, 1, 1)
            self.lrelu = nn.LeakyReLU(0.2, inplace=True)

        @property
        def scale(self):
            return (2 ** self.n_up) / self.unshuffle

        def forward(self, x):
            if self.unshuffle > 1:
                x = F.pixel_unshuffle(x, self.unshuffle)
            feat = self.conv_first(x)
            feat = feat + self.conv_body(self.body(feat))
            for i in range(self.n_up):
                conv = getattr(self, "conv_up%d" % (i + 1))
                feat = self.lrelu(conv(F.interpolate(feat, scale_factor=2, mode="nearest")))
            return self.conv_last(self.lrelu(self.conv_hr(feat)))

    class SRVGGNetCompact(nn.Module):
        """! @brief Real-ESRGAN's compact VGG-style net (PReLU, pixel shuffle, input skip)."""

        def __init__(self, in_nc=3, out_nc=3, nf=64, n_conv=16, upscale=4, act="prelu"):
            super().__init__()
            self.in_nc = in_nc
            self.out_nc = out_nc
            self.upscale = upscale
            layers = [nn.Conv2d(in_nc, nf, 3, 1, 1), self._act(act, nf)]
            for _ in range(n_conv):
                layers += [nn.Conv2d(nf, nf, 3, 1, 1), self._act(act, nf)]
            layers.append(nn.Conv2d(nf, out_nc * upscale * upscale, 3, 1, 1))
            self.body = nn.Sequential(*layers)
            self.upsampler = nn.PixelShuffle(upscale)

        @staticmethod
        def _act(kind, nf):
            if kind == "prelu":
                return nn.PReLU(num_parameters=nf)
            if kind == "lrelu":
                return nn.LeakyReLU(0.1, inplace=True)
            return nn.ReLU(inplace=True)

        @property
        def scale(self):
            return self.upscale

        def forward(self, x):
            out = self.upsampler(self.body(x))
            return out + F.interpolate(x, scale_factor=self.upscale, mode="nearest")


_OLD_RDB = re.compile(r"^model\.1\.sub\.(\d+)\.RDB(\d)\.conv(\d)\.0\.(weight|bias)$")
_OLD_TRUNK = re.compile(r"^model\.1\.sub\.(\d+)\.(weight|bias)$")
_OLD_TOP = re.compile(r"^model\.(\d+)\.(weight|bias)$")


def _convert_old_esrgan(sd):
    """! @brief Original ESRGAN keys ("model.N...") -> RRDBNet keys, plus n_up."""
    tops = sorted({int(m.group(1)) for k in sd for m in [_OLD_TOP.match(k)] if m})
    if 0 not in tops or len(tops) < 3:
        raise UnsupportedModel("not an ESRGAN checkpoint")
    # model.0 first conv, then [Upsample, conv, LReLU] per x2 at 3, 6, 9..., HR conv, LReLU, last conv
    ups = [i for i in tops if i >= 3 and i % 3 == 0 and i < tops[-2]]
    n_up = len(ups)
    if tops[-2] != 3 * n_up + 2 or tops[-1] != 3 * n_up + 4:
        raise UnsupportedModel("unrecognised ESRGAN layout (pixel-shuffle ESRGAN needs spandrel)")
    names = {0: "conv_first", tops[-2]: "conv_hr", tops[-1]: "conv_last"}
    for n, i in enumerate(ups):
        names[i] = "conv_up%d" % (n + 1)
    trunk = max((int(m.group(1)) for k in sd for m in [_OLD_TRUNK.match(k)] if m), default=None)
    if trunk is None:
        raise UnsupportedModel("ESRGAN checkpoint without a trunk conv")
    out = {}
    for k, v in sd.items():
        m = _OLD_RDB.match(k)
        if m:
            out["body.%s.rdb%s.conv%s.%s" % m.groups()] = v
            continue
        m = _OLD_TRUNK.match(k)
        if m and int(m.group(1)) == trunk:
            out["conv_body." + m.group(2)] = v
            continue
        m = _OLD_TOP.match(k)
        if m and int(m.group(1)) in names:
            out[names[int(m.group(1))] + "." + m.group(2)] = v
            continue
        raise UnsupportedModel("unexpected key %r in an ESRGAN checkpoint" % k)
    return out, n_up


def _build_rrdb(sd):
    """! @brief RRDBNet for new-layout keys (sd already converted)."""
    nb = 1 + max(int(k.split(".")[1]) for k in sd if k.startswith("body."))
    nf, in_ch = sd["conv_first.weight"].shape[:2]
    gc = sd["body.0.rdb1.conv1.weight"].shape[0]
    out_nc = sd["conv_last.weight"].shape[0]
    n_up = sum(1 for k in sd if re.match(r"^conv_up\d+\.weight$", k))
    unshuffle = next((u for u in (1, 2, 4) if in_ch == out_nc * u * u), None)
    if unshuffle is None:
        raise UnsupportedModel("RRDBNet with %d input channels" % in_ch)
    in_nc = in_ch // (unshuffle * unshuffle)
    net = RRDBNet(in_nc=in_nc, out_nc=out_nc, nf=nf, nb=nb, gc=gc, n_up=n_up, unshuffle=unshuffle)
    net.load_state_dict(sd, strict=True)
    return net


def _build_compact(sd):
    """! @brief SRVGGNetCompact from "body.N" keys."""
    convs = sorted(int(k.split(".")[1]) for k, v in sd.items()
                   if re.match(r"^body\.\d+\.weight$", k) and v.dim() == 4)
    prelus = [int(k.split(".")[1]) for k, v in sd.items()
              if re.match(r"^body\.\d+\.weight$", k) and v.dim() == 1]
    if len(convs) < 2:
        raise UnsupportedModel("not a compact SR net")
    first, last = sd["body.%d.weight" % convs[0]], sd["body.%d.weight" % convs[-1]]
    nf, in_nc = first.shape[:2]
    out_ch = last.shape[0]
    out_nc = in_nc
    up = math.isqrt(out_ch // out_nc) if out_ch % out_nc == 0 else 0
    if up < 1 or up * up * out_nc != out_ch:
        raise UnsupportedModel("compact net with %d output channels" % out_ch)
    act = "prelu" if prelus else "lrelu"
    net = SRVGGNetCompact(in_nc=in_nc, out_nc=out_nc, nf=nf, n_conv=len(convs) - 2, upscale=up, act=act)
    net.load_state_dict(sd, strict=True)    # activations carry no weights unless PReLU
    return net


def build(sd):
    """! @brief The network a state dict describes, with its weights loaded.
    @return (net, arch name). @throws UnsupportedModel.
    """
    if not HAVE_TORCH:
        raise RuntimeError("torch is not installed")
    if "conv_first.weight" in sd and "body.0.rdb1.conv1.weight" in sd:
        return _build_rrdb(sd), "RRDBNet"
    if "model.0.weight" in sd and any(k.startswith("model.1.sub.") for k in sd):
        conv, _ = _convert_old_esrgan(sd)
        return _build_rrdb(conv), "ESRGAN"
    if "body.0.weight" in sd and not any(".rdb" in k for k in sd):
        return _build_compact(sd), "SRVGGNetCompact"
    raise UnsupportedModel("unknown architecture")