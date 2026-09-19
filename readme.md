install:

```
git clone https://github.com/yggdrasil75/customimagemanager
cd customimagemanager
./install.sh
./run.sh
```

`install.sh` asks which profile you want (or takes `--profile`), installs the
system packages for your distro, builds the venv, pulls the python deps that
profile actually needs, fetches the vendored front-end JS, and writes the
module on/off map plus the chosen profile/backend into `app_config.json`. GPU
backend is detected (`--backend cpu|cuda|rocm` to force it). The resolution
lives in `modules/deps.py`, next to the rest of the module system — the
scripts are thin wrappers around it.

profiles:

| profile | what's in it |
|---|---|
| `ultralight` | viewer + metadata editing. no torch, no ML stack. |
| `light` | + the small/fast models: YOLO, MobileSAM, RTMPose, faces, rapidocr, dedup. |
| `heavy-only` | + only the large ones: SAM 2/3, DINO, pyiqa, SMPL-X, embeddings, trainer. |
| `full` | everything. |

```
./install.sh --profile light --backend cuda   # non-interactive
./run.sh --profile heavy-only                 # switch profile later
./update.sh                                   # git pull + re-sync deps
python3 modules/deps.py tiers                 # what's on/off and why
```

A module you enable yourself in Settings -> Modules gets its pip deps
installed by `run.sh` on the next start; nothing to install by hand. Three
packages aren't on pypi under a usable name (`anny`, `atlas`, `shapy`) — those
modules stay off until you install them yourself, and say so in the Modules
tab.

Windows: use docker, or WSL.

alternatively:

```
docker compose up --build
```

with future runs just needing (outside of major updates):

```
docker compose up
```

what it is:

no image manager met my needs while being compatible with linux, not a monthly fee, and fast.

so, make my own.

current features:
majority of iptc, exif, and official/native xmp fields have an editor.

all edits are written to the file.

everything but embeddings are written to the file automatically. (embeddings arent because they are unreasonably big and changing settings needs to flush them anyway)

generate a yolo model based on your own boxes on the fly.
bounding boxes for both the person and the face
pose generation as well.

easy to edit description field
set your own image tags.
set tags per person in the image as well.
ai pipeline tagging (let gemma try to make bounds for you)

has: detect, classify, pose.
todo: segment, semantic, obb, track. 
improve:  
pose uses wholebody, which is good for limited occlusion. need to use crowdpose for heavy occlusion. and an automatic switch.  
switch from coco-2017 based yolo to objects365 based. or openimages v7 based.



storage tiering:
allow you to set nvme storage for thumbnails, hdd for videos, potentially slower hdds for low bitrate videos