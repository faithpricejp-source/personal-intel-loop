from personal_intel_loop import vault_corpus as vc


def test_build_filter_none_when_no_constraints():
    assert vc._build_filter(None, None) is None
    assert vc._build_filter(["", None], []) is None


def test_build_filter_maps_object_types_and_layers_to_match_any():
    flt = vc._build_filter(["mechanism", "heuristic"], ["05 知识本体"])
    conds = {c.key: c.match.any for c in flt.must}
    assert conds == {"object_type": ["mechanism", "heuristic"], "layer": ["05 知识本体"]}


def test_missing_marker_raises_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(vc, "_CLIENT", None)
    monkeypatch.setattr(vc, "_COLLECTION_NAME", None)
    try:
        vc._load_collection(marker=tmp_path / "absent.txt")
    except vc.VaultCorpusStale:
        return
    raise AssertionError("expected VaultCorpusStale")
