"""Pipeline module: applying an analysis to a file's metadata."""
import pytest
from cimtest import read_meta, write_meta, box, media_path

from modules.pipeline import pipeline_core as pc


def _analysis(**kw):
    a = {"tags": ["outdoor", "?street"], "summary": "a man", "subjects": [
        {"label": "person", "name": "", "box": {"cx": .5, "cy": .55, "w": .4, "h": .8},
         "description": "man in coat", "tags": ["coat"]}]}
    a.update(kw)
    return a


def test_apply_writes_tags_description_subject(client, upload):
    fn = upload(seed=501)
    tags, desc, regions = pc._apply_pipeline_result(media_path(fn), _analysis())
    m = read_meta(client, fn)
    assert {"?outdoor", "?street"} <= set(m["tags"]), "AI tags land unconfirmed"
    assert "a man" in m["description"]
    subj = [r for r in m["regions"] if r["class_name"] == "person"]
    assert len(subj) == 1 and subj[0]["region_type"] == "person" and subj[0]["confirmed"] is False
    assert [t["tag"] for t in subj[0]["region_tags"]] == ["coat"]


def test_existing_tags_not_duplicated(client, upload):
    fn = upload(seed=502)
    write_meta(client, fn, tags=["outdoor"])
    pc._apply_pipeline_result(media_path(fn), _analysis())
    names = [t.lstrip("?").lower() for t in read_meta(client, fn)["tags"]]
    assert names.count("outdoor") == 1


@pytest.mark.xfail(strict=True, reason="known: pipeline appends subjects without matching "
                   "existing boxes -> duplicate person boxes. Remove this marker once fixed.")
def test_subject_reuses_existing_person_box(client, upload):
    fn = upload(seed=503)
    write_meta(client, fn, regions=[box(region_name="jill", confirmed=True)])
    pc._apply_pipeline_result(media_path(fn), _analysis())
    persons = [r for r in read_meta(client, fn)["regions"] if r["class_name"] == "person"]
    assert len(persons) == 1, f"{len(persons)} person boxes after the pipeline ran"


def test_route_requires_llm(client, upload, app, ungated):
    fn = upload(seed=504)
    saved = app.state.get("oai_endpoint")
    app.state["oai_endpoint"] = ""
    try:
        j = client.post("/api/run_pipeline", json={"filename": fn}).get_json()
        assert j["success"] is False and "LLM" in j["error"]
    finally:
        app.state["oai_endpoint"] = saved
