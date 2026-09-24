#!/usr/bin/env python3
"""Download the weights for every model the app knows, up front.

    python prefetch_models.py                 every capability, every size/type
    python prefetch_models.py pose segment    only these capabilities
    python prefetch_models.py --list          show what would be fetched, fetch nothing
    python prefetch_models.py --remote        include endpoint-backed models (they
                                              have nothing to download; this only
                                              checks they answer)

Runs from the repo root, against the same models/ folder and app_config.json
the app uses. Every provider is bound once per size/type it declares — that is
what triggers a provider's own download — then dropped again, so only one
model is resident at a time. Weights already on disk are not re-fetched.

Exit status is the number of models that failed, and the summary at the end
names each one with its error, so this doubles as "can every model load here".
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)


def _variants(p):
    sizes = p.sizes or [None]
    types = [t["value"] for t in p.types] or [None]
    return [(s, t) for s in sizes for t in types]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("caps", nargs="*", help="capabilities to fetch (default: all)")
    ap.add_argument("--list", action="store_true", help="only list what would be fetched")
    ap.add_argument("--remote", action="store_true", help="include endpoint-backed models")
    args = ap.parse_args()

    import logging
    logging.disable(logging.WARNING)                 # registration chatter
    import manager                                   # noqa: E402  (boots the app headless)
    import model_registry
    from model_registry import REGISTRY

    b = manager.module_host.broker
    want = set(args.caps)
    jobs = []
    for cap, provs in sorted(b._providers.items()):
        if want and cap not in want:
            continue
        for pid, p in sorted(provs.items()):
            if p.resource and not args.remote:
                continue
            if not p.available():
                jobs.append((cap, pid, None, None, f"unavailable: {p.reason()}"))
                continue
            for size, typ in _variants(p):
                jobs.append((cap, pid, size, typ, None))

    if want and not jobs:
        print(f"no models for {sorted(want)}; capabilities are: {sorted(b._providers)}")
        return 2

    width = max(len(f"{c}:{pid}[{s or ''}/{t or ''}]") for c, pid, s, t, _ in jobs)
    if args.list:
        for cap, pid, size, typ, skip in jobs:
            tag = f"{cap}:{pid}[{size or ''}/{typ or ''}]"
            print(f"  {tag:<{width}}  {'skip: ' + skip if skip else 'fetch'}")
        return 0

    failed = []
    for cap, pid, size, typ, skip in jobs:
        tag = f"{cap}:{pid}" + (f"[{'/'.join(x for x in (size, typ) if x)}]" if size or typ else "")
        if skip:
            print(f"  {tag:<{width}}  --  {skip}")
            continue
        prev = dict(b._variant.get(cap, {}))
        b._variant[cap] = {**prev, **({"size": size} if size else {}), **({"type": typ} if typ else {})}
        t0 = time.time()
        try:
            b.request(cap, provider=pid)            # binding is what downloads
            print(f"  {tag:<{width}}  ok  {time.time() - t0:5.1f}s", flush=True)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}".splitlines()[0][:160]
            failed.append((tag, msg))
            print(f"  {tag:<{width}}  FAIL {msg}", flush=True)
        finally:
            b._variant[cap] = prev
            try:
                REGISTRY.clear()                    # one model resident at a time
            except Exception:
                pass

    print()
    print(f"{len(jobs) - len(failed)} ok, {len(failed)} failed")
    for tag, msg in failed:
        print(f"  {tag}: {msg}")
    return len(failed)


if __name__ == "__main__":
    sys.exit(main())