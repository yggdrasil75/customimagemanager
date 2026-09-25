"""
Datasets fetcher module.
======================================================================
Registers a "datasets" FETCHER into the fetch module's registry and a
Settings > Datasets tab. Closed zoos (pyiqa's IQA benchmark mirror, the
dataset YAMLs shipped with the installed ultralytics) are one-click
downloads there; open-ended hosts take a link (Hugging Face, Kaggle,
Zenodo, or any archive URL). See ds.py for the target grammar.

Nothing goes into the library: a dataset lands in <media>/.datasets/<name>/
(a dot folder, so the library scan skips it), archives are extracted,
parquet image columns are written out as files, and labels become
<name>/labels.csv. The folder is then added to the Dedup trainer's dataset
folders and, when it has labels, to the IQA trainer's datasets, so both
pretrainers pick it up on their next build.
"""
import os

from flask import jsonify
from werkzeug.utils import secure_filename

from . import ds

MANIFEST = {
    "id":          "datasets",
    "name":        "Datasets fetcher",
    "version":     "1.1.0",
    "description": "Download training datasets (pyiqa / ultralytics zoos, Hugging Face, Kaggle, "
                   "Zenodo, archive URLs) into media/.datasets/ for the dedup / IQA pretrainers.",
    "core":        False,
    "requires":    ["fetch"],
    "pip":         [],
    "assets":      ["datasets.js"],
}


def _add_line(text, line, folder):
    """Append line to a newline list unless a line for folder is already there."""
    lines = [l for l in str(text or "").splitlines() if l.strip()]
    if any(l.split()[0] == folder for l in lines):
        lines = [line if l.split()[0] == folder else l for l in lines]
    else:
        lines.append(line)
    return "\n".join(lines)


def register(host):
    fetch = host.get_service("fetch")
    if fetch is None:
        host.logger.info("datasets: fetch service unavailable; not registering")
        return
    cfg = host.config
    root = os.path.join(host.media_dir, ".datasets")

    host.add_settings_tab("datasets", "Datasets", icon="\U0001F4E6")
    host.add_asset("datasets.js")
    for key, label, help_ in (
            ("datasets_hf_token", "Hugging Face token", "Only needed for gated or private Hugging Face datasets."),
            ("datasets_kaggle_user", "Kaggle username", "Kaggle downloads need your username + API key "
                                                        "(kaggle.com > Settings > API)."),
            ("datasets_kaggle_key", "Kaggle API key", None)):
        host.add_config_key(key, default="", validate=lambda v: str(v or "").strip())
        host.add_settings_field(key=key, label=label, kind="text", pane="datasets", help=help_)

    def _creds():
        return {"hf": cfg.get("datasets_hf_token"), "kaggle_user": cfg.get("datasets_kaggle_user"),
                "kaggle_key": cfg.get("datasets_kaggle_key")}

    def _say(msg):
        cfg["status_text"] = "Datasets: " + msg

    def _register_dataset(folder, labels):
        cfg["dedup_train_folders"] = _add_line(cfg.get("dedup_train_folders"), folder, folder)
        if labels:
            cfg["iqa_train_datasets"] = _add_line(cfg.get("iqa_train_datasets"),
                                                  f"{folder} {labels}", folder)
        host.save_config()

    def _fetch(target, tmpdir, on_file=None):
        spec = ds.parse_target(target)
        name = secure_filename(spec["name"]) or "dataset"
        dest = os.path.join(root, name)
        _say(f"fetching {name}...")
        labels = None
        gen = ds.fetch(spec, dest, creds=_creds())
        try:
            while True:
                next(gen)
                yield "", {}                          # lets the queue worker cancel between chunks
        except StopIteration as done:
            labels = done.value
        n = sum(1 for _dp, _dn, fns in os.walk(dest) for f in fns
                if os.path.splitext(f)[1].lower() in ds.IMG_EXTS)
        _register_dataset(dest, labels)
        _say(f"{name}: {n} images" + (f", labels {os.path.basename(labels)}" if labels else ", no labels")
             + " (added to the trainers' dataset lists)")

    fetch.register({
        "id": "datasets", "label": "Datasets",
        "available": lambda: True,
        "handles": lambda t: bool(t) and t.strip().lower().startswith(ds.PREFIX),
        "target_key": lambda t: "dataset:" + ds.host_of(ds.parse_target(t)),
        "fetch": _fetch,
    })

    def api_zoo():
        have = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))) \
            if os.path.isdir(root) else []
        return jsonify({"success": True, "zoos": ds.zoo(), "have": have, "root": root})

    host.add_route("/api/datasets/zoo", api_zoo, endpoint="datasets_zoo", feature="fetch")
    host.logger.info("datasets: registered fetcher + Settings > Datasets")