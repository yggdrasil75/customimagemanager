install:

```
git clone https://github.com/yggdrasil75/customimagemanager
cd customimagemanager
./install.sh
./run.sh
```

`install.sh` installs the system packages, builds the venv, installs the
python deps and fetches the vendored front-end JS. Modes are the docker ones:

```
./install.sh                 # detect GPU (cuda/rocm/cpu), full deps
./install.sh ultralight      # viewer + metadata only: no torch, no ML
./install.sh cuda            # force a backend: cpu | cuda | rocm
./install.sh cpu --minimal   # backend wheels only, nothing optional
./update.sh                  # git pull + reinstall that mode's deps
```

Modules are enabled and disabled in Settings -> Modules, and enabling one
installs its declared pip deps for you — that is the whole procedure, no
requirements file to hunt down. torch / torchvision / onnxruntime are the
exception: they come from `requirements-<backend>.txt` so they resolve against
the right wheel index, so a module needing them says so and waits for
`./install.sh cpu|cuda|rocm`. `CIM_NO_AUTO_INSTALL=1` turns the auto-install
off. So `--minimal` plus toggles is the "light" install, and a module whose
dep won't install just reports it and stays off.

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