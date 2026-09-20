from website_test_pipeline.sitemap import build_site_map, load_inventories, render_site_map

def _inv(url, controls=(), revealed=(), primary=None, forms=(), embeds=()):
    return {"url": url, "title": "T " + url, "headings": [{"text": "Main", "hidden": False, "in_feed": False}],
            "controls": list(controls), "revealed": list(revealed), "primary_flow": primary,
            "forms": list(forms), "embeds": list(embeds)}

def _link(name, href, region="other"):
    return {"tag": "a", "name": name, "href": href, "region": region}

HOME = _inv("https://x.test/en",
            controls=[_link("Find", "/en/find", "chrome"), _link("Map", "/en/map"), _link("Away", "https://other.test/"),
                      _link("Top", "#top"), {"tag": "button", "name": "Search", "region": "other"},
                      {"tag": "select", "name": "Country", "selector": "#c", "region": "other", "options": ["1", "2"]},
                      {"tag": "button", "name": "Hidden", "region": "other", "hidden": True}],
            revealed=[{"trigger": "Search", "effect": "navigates", "to": "https://x.test/en/find"},
                      {"trigger": "Menu", "effect": "reveals", "controls": [{}, {}]}],
            primary={"action": "Search", "effect": "navigates", "to": "https://x.test/en/find?country=A"})
FIND = _inv("https://x.test/en/find", controls=[_link("Find", "/en/find", "chrome")])

def test_page_summary_keeps_only_real_content_actions():
    site = build_site_map([HOME, FIND])
    home = next(p for p in site["pages"] if p["path"] == "/en")
    names = [(a["kind"], a["name"]) for a in home["actions"]]
    assert ("link", "Map") in names and ("button", "Search") in names and ("select", "#c") in names
    assert not any(n in {"Away", "Top", "Hidden"} for _, n in names)
    assert next(a for a in home["actions"] if a["kind"] == "select")["options"] == 2

def test_edges_come_from_links_reveals_and_probe_and_flag_unexplored():
    site = build_site_map([HOME, FIND])
    edges = {(e["from"], e["to"]): e for e in site["edges"]}
    assert edges[("/en", "/en/map")]["explored"] is False
    assert edges[("/en", "/en/find")]["explored"] is True
    assert ("/en", "/en/find?country=A") in edges

def test_shared_nav_shown_once():
    site = build_site_map([HOME, FIND])
    assert site["nav"] == [{"name": "Find", "to": "/en/find"}]
    assert all("Find" not in [a["name"] for a in p["actions"]] for p in site["pages"])

def test_render_is_compact_and_mentions_probe_and_reveals():
    text = render_site_map(build_site_map([HOME, FIND]))
    assert text.startswith("SITE MAP (2 pages explored)")
    assert 'clicking "Menu" reveals 2 control(s)' in text
    assert "probe: Search -> navigates /en/find?country=A" in text
    assert "[not explored]" in text

def test_render_truncates_at_a_line_boundary():
    text = render_site_map(build_site_map([HOME, FIND]), max_chars=120)
    assert len(text) < 200 and text.endswith("(truncated)")

def test_load_inventories_skips_corrupt_files(tmp_path):
    (tmp_path / "a.inventory.json").write_text('{"url": "https://x.test/"}', encoding="utf-8")
    (tmp_path / "b.inventory.json").write_text("{ nope", encoding="utf-8")
    assert [i["url"] for i in load_inventories(tmp_path)] == ["https://x.test/"]
