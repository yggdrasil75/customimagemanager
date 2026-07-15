install:

clone this repo

save this file to static as tailwindcss.js: https://cdn.tailwindcss.com/3.4.17
```
apt install build-essential libjxl-dev
git clone https://github.com/yggdrasil75/customimagemanager
cd customimagemanager
wget https://cdn.tailwindcss.com/3.4.17 -O static/tailwindcss.js
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python manager.py
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


storage tiering:
allow you to set nvme storage for thumbnails, hdd for videos, potentially slower hdds for low bitrate videos