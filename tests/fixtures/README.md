# Test fixture media

Drop real files here with EXACTLY these names. Every test that needs one calls
`fixture("name")` and is SKIPPED (not failed) when the file is absent, so the
suite passes with an empty folder and gets stricter as you add files.
Nothing here is committed by default (see .gitignore) — keep it local.

Sidecar `.txt` files hold the expected result for content tests (one line,
exact text). They are optional; the test only checks what you provide.

| file | what it must contain | used by |
|---|---|---|
| `person_single.jpg` | ONE person, full body, face clearly visible, standing | people detect, faces, bodies, pose, segmentation, region merge |
| `person_multi.jpg` | TWO OR MORE people, all faces visible | people detect (count ≥ 2), face clustering |
| `face_closeup.jpg` | one face filling most of the frame, no body | face detect / embed |
| `same_person_a.jpg` | person X, photo 1 | face clustering: a+b must land in one cluster |
| `same_person_b.jpg` | person X, photo 2 (different day/outfit/angle) | face clustering |
| `other_person.jpg` | a DIFFERENT person than X, face visible | face clustering: must NOT cluster with a/b |
| `no_person.jpg` | landscape / object, zero people, zero faces, zero text | negatives for every detector |
| `near_dup_a.jpg` | any photo | dedup: a and b must be reported as near-duplicates |
| `near_dup_b.jpg` | the SAME photo as near_dup_a, resized ~80% and/or re-saved at lower JPEG quality | dedup |
| `barcode_qr.jpg` | a clean QR code; put its decoded text in `barcode_qr.txt` | barcodes |
| `barcode_1d.jpg` | an EAN-13 / Code-128 barcode; decoded digits in `barcode_1d.txt` | barcodes |
| `text_document.jpg` | a photo/scan of printed text (≥ 3 lines); a phrase that MUST appear in the OCR output in `text_document.txt` | ocr |
| `animated.gif` | short animation, ≥ 4 frames, < 5 s | animated-JXL ingest, keyframes, `is_animated` |
| `clip.mp4` | 2–10 s H.264 video, ≥ 1 person moving | video ingest, poster frame, video tracks / detect |
| `photo_exif.jpg` | straight-off-camera JPEG with EXIF (date taken, camera, GPS if you like) | exif import / export, date search |
| `photo_with_xmp.jpg` + `photo_with_xmp.xmp` | a photo plus an XMP sidecar written by Lightroom/digiKam/ACDSee with face regions | region import from foreign mwg-rs / ACDSee |
| `raw.dng` | any small raw file (DNG ok) | raw develop path (optional, slow) |
| `book.epub` | any DRM-free epub with a cover | books module (optional) |
| `comic.cbz` | a zip of 3+ page images renamed .cbz | comics module (optional) |
| `song.mp3` | any short mp3 with ID3 tags (title/artist) | music module (optional) |

Keep images ≤ ~2000 px on the long side so the model tests stay fast.
