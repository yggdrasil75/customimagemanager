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

## Models

Every model registered with the broker is tested automatically — adding one
means no test edits:

    ./run_tests.sh tests/test_providers.py              every model, once each
    ./run_tests.sh tests/test_providers.py -k "pose:"   one capability
    ./run_tests.sh tests/test_providers.py -k rtmw      one model
    ./run_tests.sh --cim-all-variants pose              every pose model exhaustively:
                                                        every size and type it declares
    ./run_tests.sh --cim-all-variants box,depth         the same, two capabilities
    ./run_tests.sh --cim-all-variants all               every capability

The question these ask is whether the program can USE a model: the image
reaches it in the form it wants, it returns without raising, and what comes
back fits the capability's contract so the consumer doesn't choke. They are
not accuracy tests — a skeleton a few pixels off still passes.

The run ends with a "model results" table: one line per model, `ok`, the first
failure, or `--` when it never ran (not installed, weights missing, endpoint
model without `--cim-remote`).

A model that deviates from its capability's contract on purpose gets an entry
in `tests/model_expectations.json` (`skip` / `exempt` / `xfail`, per model or
per test) — data, not code.

Model tests use your downloaded weights (models/ is linked, not copied), and
only one model is held in memory at a time.
Module tests live in `modules/<id>/tests/` (see `modules/example_hello/tests/`
for a Python and a JS template).
