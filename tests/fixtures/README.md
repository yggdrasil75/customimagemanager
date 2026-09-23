# Test fixture media

Drop real files here with these names (or keep them elsewhere and run
`./run_tests.sh --cim-fixtures /path/to/dir`). The EXTENSION doesn't matter —
`barcode_qr.jpg` satisfies `barcode_qr.png`, `clip.mkv` satisfies `clip.mp4`.
A test that needs a missing file is SKIPPED, not failed, so the suite gets
stricter as you add files. Nothing here is committed (see .gitignore) except
this README.

`<name>.txt` next to a fixture says what must be read out of it:
  * `barcode_qr.txt` / `barcode_1d.txt` — the exact payload, one line.
  * `text_document.txt` — either a short phrase that must appear verbatim
    (best: it pins the OCR to one spot), or the whole page's text, which is
    matched loosely (60% of its words must be read, since OCR differs on
    layout and reading order).

`./run_tests.sh tests/test_fixtures.py` checks the fixtures themselves with
your picked models: whether person_single really holds one detectable person,
whether the near-dup pair is the same picture, and so on. Run it first when a
lot of model tests fail the same way — it's usually the photo, not the model.

| file | must contain | used by |
|---|---|---|
| `person_single.jpg` | ONE person, full body, standing, face visible | test_providers (persons, pose, segment, iqa), people, pose, segmentation, rating |
| `person_multi.jpg` | TWO OR MORE people, faces visible | test_providers (persons ≥2, faces ≥2, pose ≥2, conf filter) |
| `face_closeup.jpg` | one face filling most of the frame | test_providers (faces) |
| `same_person_a.jpg` | person X, photo 1 | test_providers + faces (identity) |
| `same_person_b.jpg` | person X, a different photo | test_providers + faces (identity) |
| `other_person.jpg` | a different person, face visible | test_providers + faces (identity) |
| `no_person.jpg` | no people, no faces, no text, no codes | negatives for every detector |
| `near_dup_a.jpg` | any photo | dedup, embedding, test_providers (embed) |
| `near_dup_b.jpg` | near_dup_a resized ~80% and/or re-saved at lower JPEG quality | dedup, embedding, test_providers (embed) |
| `barcode_qr.png` + `barcode_qr.txt` | a clean QR code; its decoded text | barcodes, test_providers |
| `barcode_1d.png` + `barcode_1d.txt` | an EAN-13 / Code-128 barcode; its digits | barcodes, test_providers |
| `text_document.jpg` + `text_document.txt` | printed text, ≥ 3 lines; a phrase that must be read | ocr, test_providers |
| `animated.gif` | ≥ 4 frames, < 5 s | core ingest |
| `clip.mp4` | 2–10 s H.264, a person moving | core ingest, video tracks |
| `photo_exif.jpg` | camera JPEG with EXIF (DateTimeOriginal at least) | core ingest, metadata |
| `photo_with_xmp.jpg` + `photo_with_xmp.xmp` | a photo + XMP sidecar from Lightroom/digiKam/ACDSee with face regions | core ingest (foreign regions) |
| `book.epub` | DRM-free epub with title, author, cover | books |
| `comic.cbz` | zip of ≥ 3 page images | comics |
| `song.mp3` | short mp3 that HAS ID3 title + artist (untagged is skipped) | music |

Keep images ≤ ~2000 px on the long side so model tests stay fast.

## Running

    ./run_tests.sh                                  everything (models included: slow)
    ./run_tests.sh --cim-no-models                  skip everything that loads a model
    ./run_tests.sh modules/people                   one module
    ./run_tests.sh tests/test_providers.py -k pose  one capability's providers
    ./run_tests.sh --cim-config ./app_config.json   start from your real config
                                                    (endpoints, weights, model picks)
    ./run_tests.sh --cim-remote                     also call endpoint-backed models
                                                    (vision LLM, OAI embeddings)

Model tests use your downloaded weights (models/ is linked, not copied).
Module tests live in `modules/<id>/tests/` (see `modules/example_hello/tests/`
for a Python and a JS template).
